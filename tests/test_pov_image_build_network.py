"""The POV image-build network (pov_cloud_aws.ensure_image_build_subnet).

  * **Find-or-create per piece.** A run that failed half way (a VPC with no subnet, an
    internet gateway never attached, no default route) is finished by the next one,
    never answered with a second VPC: an account has five per region by default.
  * **One preparer per region.** Two builds starting together find one network.

Runs under pytest, or standalone:  python tests/test_pov_image_build_network.py
"""
import asyncio
import os
import sys
import tempfile

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)
os.environ.setdefault("DATABASE_URL", "sqlite:///" + os.path.join(
    tempfile.mkdtemp(prefix="pov-build-net-"), "t.db").replace("\\", "/"))
os.environ.setdefault("JWT_SECRET_KEY", "x" * 32)

try:
    from web_dashboard.services import aws_service, pov_cloud_aws
except ModuleNotFoundError as exc:  # pragma: no cover — no app deps installed
    print(f"SKIP: {exc}")
    sys.exit(0)


def _tagged(spec_list):
    return {t["Key"]: t["Value"] for t in spec_list[0]["Tags"]} if spec_list else {}


class _Ec2:
    """Just enough EC2 for the image-build network, recording every create."""

    def __init__(self):
        self.vpcs, self.subnets, self.igws, self.rts, self.created = [], [], [], [], []
        self._n = 0

    def _id(self, kind):
        self._n += 1
        return f"{kind}-{self._n:04d}"

    @staticmethod
    def _match(tags, filters):
        for f in filters or []:
            if f["Name"].startswith("tag:") and tags.get(f["Name"][4:]) not in f["Values"]:
                return False
        return True

    def describe_subnets(self, Filters=None):
        vpc = next((f["Values"][0] for f in Filters or [] if f["Name"] == "vpc-id"), None)
        return {"Subnets": [s for s in self.subnets if self._match(s["Tags"], Filters)
                            and (vpc is None or s["VpcId"] == vpc)]}

    def describe_vpcs(self, Filters=None):
        return {"Vpcs": [v for v in self.vpcs if self._match(v["Tags"], Filters)]}

    def create_vpc(self, CidrBlock, TagSpecifications):
        vpc = {"VpcId": self._id("vpc"), "Tags": _tagged(TagSpecifications)}
        self.vpcs.append(vpc)
        self.rts.append({"RouteTableId": self._id("rtb"), "VpcId": vpc["VpcId"], "Routes": []})
        self.created.append("vpc")
        return {"Vpc": vpc}

    def modify_vpc_attribute(self, **kw):
        return {}

    def create_subnet(self, VpcId, CidrBlock, TagSpecifications):
        sub = {"SubnetId": self._id("subnet"), "VpcId": VpcId, "State": "available",
               "Tags": _tagged(TagSpecifications)}
        self.subnets.append(sub)
        self.created.append("subnet")
        return {"Subnet": sub}

    def modify_subnet_attribute(self, **kw):
        return {}

    def describe_internet_gateways(self, Filters=None):
        out = []
        for g in self.igws:
            vpc = next((f["Values"][0] for f in Filters or []
                        if f["Name"] == "attachment.vpc-id"), None)
            if vpc is not None:
                if any(a["VpcId"] == vpc for a in g["Attachments"]):
                    out.append(g)
            elif self._match(g["Tags"], Filters):
                out.append(g)
        return {"InternetGateways": out}

    def create_internet_gateway(self, TagSpecifications):
        g = {"InternetGatewayId": self._id("igw"), "Attachments": [],
             "Tags": _tagged(TagSpecifications)}
        self.igws.append(g)
        self.created.append("igw")
        return {"InternetGateway": g}

    def attach_internet_gateway(self, InternetGatewayId, VpcId):
        next(g for g in self.igws if g["InternetGatewayId"] == InternetGatewayId)[
            "Attachments"].append({"VpcId": VpcId})

    def describe_route_tables(self, Filters=None):
        vpc = next(f["Values"][0] for f in Filters if f["Name"] == "vpc-id")
        return {"RouteTables": [r for r in self.rts if r["VpcId"] == vpc]}

    def create_route(self, RouteTableId, DestinationCidrBlock, GatewayId):
        next(r for r in self.rts if r["RouteTableId"] == RouteTableId)["Routes"].append(
            {"DestinationCidrBlock": DestinationCidrBlock, "GatewayId": GatewayId})
        self.created.append("route")

    def create_tags(self, **kw):
        return {}

    def delete_vpc(self, VpcId):
        self.vpcs = [v for v in self.vpcs if v["VpcId"] != VpcId]
        self.rts = [r for r in self.rts if r["VpcId"] != VpcId]
        self.created.append("delete-vpc")

    def describe_instances(self, Filters=None):
        vpc = next(f["Values"][0] for f in Filters if f["Name"] == "vpc-id")
        return {"Reservations": [{"Instances": [i for i in getattr(self, "instances", [])
                                                if i["VpcId"] == vpc]}]}

    def detach_internet_gateway(self, InternetGatewayId, VpcId):
        g = next(g for g in self.igws if g["InternetGatewayId"] == InternetGatewayId)
        g["Attachments"] = [a for a in g["Attachments"] if a["VpcId"] != VpcId]

    def delete_internet_gateway(self, InternetGatewayId):
        self.igws = [g for g in self.igws if g["InternetGatewayId"] != InternetGatewayId]
        self.created.append("delete-igw")

    def delete_subnet(self, SubnetId):
        self.subnets = [s for s in self.subnets if s["SubnetId"] != SubnetId]
        self.created.append("delete-subnet")

    def replace_route(self, RouteTableId, DestinationCidrBlock, GatewayId):
        rt = next(r for r in self.rts if r["RouteTableId"] == RouteTableId)
        rt["Routes"] = [r for r in rt["Routes"] if r["DestinationCidrBlock"] != DestinationCidrBlock]
        rt["Routes"].append({"DestinationCidrBlock": DestinationCidrBlock,
                             "GatewayId": GatewayId, "State": "active"})
        self.created.append("replace-route")


def _with(ec2):
    saved = aws_service._get_ec2_pinned
    aws_service._get_ec2_pinned = lambda region: ec2
    return lambda: setattr(aws_service, "_get_ec2_pinned", saved)


def test_the_first_build_creates_one_network():
    ec2 = _Ec2()
    restore = _with(ec2)
    try:
        sub = pov_cloud_aws._ensure_image_build_subnet_sync("us-east-2")
        assert sorted(ec2.created) == ["igw", "route", "subnet", "vpc"]
        assert pov_cloud_aws._ensure_image_build_subnet_sync("us-east-2") == sub
        assert sorted(ec2.created) == ["igw", "route", "subnet", "vpc"], "reused"
    finally:
        restore()


def test_a_half_built_network_is_finished_not_duplicated():
    ec2 = _Ec2()
    tags = pov_cloud_aws._image_build_spec("vpc", "x")
    # A run died right after create_vpc: a VPC, no subnet, no gateway, no route.
    ec2.create_vpc(CidrBlock="10.254.0.0/16", TagSpecifications=[tags])
    ec2.created.clear()
    restore = _with(ec2)
    try:
        pov_cloud_aws._ensure_image_build_subnet_sync("us-east-2")
    finally:
        restore()
    assert len(ec2.vpcs) == 1, "no second VPC"
    assert sorted(ec2.created) == ["igw", "route", "subnet"]
    assert ec2.igws[0]["Attachments"] == [{"VpcId": ec2.vpcs[0]["VpcId"]}]


def test_an_unattached_gateway_of_ours_is_attached_not_recreated():
    ec2 = _Ec2()
    restore = _with(ec2)
    try:
        sub = pov_cloud_aws._ensure_image_build_subnet_sync("us-east-2")
        g = ec2.igws[0]
        g["Attachments"].clear()
        ec2.rts[0]["Routes"].clear()
        ec2.created.clear()
        assert pov_cloud_aws._ensure_image_build_subnet_sync("us-east-2") == sub
    finally:
        restore()
    assert ec2.created == ["route"] and len(ec2.igws) == 1
    assert g["Attachments"] == [{"VpcId": ec2.vpcs[0]["VpcId"]}]


def test_builds_starting_together_find_one_network():
    ec2 = _Ec2()
    restore = _with(ec2)

    async def both():
        return await asyncio.gather(pov_cloud_aws.ensure_image_build_subnet("eu-west-1"),
                                    pov_cloud_aws.ensure_image_build_subnet("eu-west-1"))
    try:
        first, second = asyncio.run(both())
    finally:
        restore()
    assert first == second and len(ec2.vpcs) == 1



def test_a_replica_that_lost_the_vpc_race_removes_its_own():
    ec2 = _Ec2()
    real_create = ec2.create_vpc

    def racing_create(**kw):
        # Another replica's VPC lands between this one's look and its create, and sorts first.
        ec2.vpcs.append({"VpcId": "vpc-0000", "Tags": _tagged(kw["TagSpecifications"])})
        ec2.rts.append({"RouteTableId": "rtb-0000", "VpcId": "vpc-0000", "Routes": []})
        return real_create(**kw)
    ec2.create_vpc = racing_create
    restore = _with(ec2)
    try:
        pov_cloud_aws._ensure_image_build_subnet_sync("us-east-2")
    finally:
        restore()
    assert [v["VpcId"] for v in ec2.vpcs] == ["vpc-0000"], "one VPC left: the winner's"
    assert "delete-vpc" in ec2.created
    assert ec2.subnets[0]["VpcId"] == "vpc-0000"


def test_a_default_route_to_a_deleted_gateway_is_repointed():
    ec2 = _Ec2()
    restore = _with(ec2)
    try:
        sub = pov_cloud_aws._ensure_image_build_subnet_sync("us-east-2")
        # Someone deleted the gateway: the route is a blackhole and nothing is attached.
        ec2.igws.clear()
        ec2.rts[0]["Routes"] = [{"DestinationCidrBlock": "0.0.0.0/0", "GatewayId": "igw-gone",
                                 "State": "blackhole"}]
        ec2.created.clear()
        assert pov_cloud_aws._ensure_image_build_subnet_sync("us-east-2") == sub
    finally:
        restore()
    route = ec2.rts[0]["Routes"][0]
    assert ec2.created == ["igw", "replace-route"]
    assert route["GatewayId"] == ec2.igws[0]["InternetGatewayId"] and route["State"] == "active"



def _extra_network(ec2, vpc_id):
    """A second complete image-build network, as two replicas that missed each other
    would leave: VPC, subnet, attached gateway."""
    tags = pov_cloud_aws._image_build_spec("vpc", "x")
    ec2.vpcs.append({"VpcId": vpc_id, "Tags": _tagged([tags])})
    ec2.rts.append({"RouteTableId": "rtb-" + vpc_id, "VpcId": vpc_id, "Routes": []})
    ec2.subnets.append({"SubnetId": "subnet-" + vpc_id, "VpcId": vpc_id, "State": "available",
                        "Tags": _tagged([pov_cloud_aws._image_build_spec("subnet", "x")])})
    ec2.igws.append({"InternetGatewayId": "igw-" + vpc_id,
                     "Attachments": [{"VpcId": vpc_id}], "Tags": {}})


def test_an_extra_network_is_removed_and_the_lowest_kept():
    ec2 = _Ec2()
    restore = _with(ec2)
    try:
        sub = pov_cloud_aws._ensure_image_build_subnet_sync("us-east-2")
        _extra_network(ec2, "vpc-9999")
        assert pov_cloud_aws._ensure_image_build_subnet_sync("us-east-2") == sub
    finally:
        restore()
    assert [v["VpcId"] for v in ec2.vpcs] == ["vpc-0001"]
    assert all(s["VpcId"] == "vpc-0001" for s in ec2.subnets)
    assert "igw-vpc-9999" not in [g["InternetGatewayId"] for g in ec2.igws]


def test_an_extra_network_with_a_running_instance_is_left_alone():
    ec2 = _Ec2()
    restore = _with(ec2)
    try:
        pov_cloud_aws._ensure_image_build_subnet_sync("us-east-2")
        _extra_network(ec2, "vpc-9999")
        ec2.instances = [{"VpcId": "vpc-9999", "State": {"Name": "running"}}]
        pov_cloud_aws._ensure_image_build_subnet_sync("us-east-2")
    finally:
        restore()
    assert sorted(v["VpcId"] for v in ec2.vpcs) == ["vpc-0001", "vpc-9999"]


def test_builds_pick_the_lowest_vpcs_subnet_even_when_another_subnet_id_sorts_first():
    ec2 = _Ec2()
    restore = _with(ec2)
    try:
        sub = pov_cloud_aws._ensure_image_build_subnet_sync("us-east-2")
        _extra_network(ec2, "vpc-9999")
        ec2.subnets[-1]["SubnetId"] = "subnet-0000"       # sorts before the winner's
        assert pov_cloud_aws._ensure_image_build_subnet_sync("us-east-2") == sub
    finally:
        restore()

if __name__ == "__main__":
    fns = [v for k, v in list(globals().items()) if k.startswith("test_") and callable(v)]
    failures = 0
    for fn in fns:
        try:
            fn()
            print(f"ok   {fn.__name__}")
        except Exception as e:  # noqa: BLE001
            failures += 1
            print(f"FAIL {fn.__name__}: {e!r}")
    print(f"\n{len(fns) - failures}/{len(fns)} passed")
    sys.exit(1 if failures else 0)
