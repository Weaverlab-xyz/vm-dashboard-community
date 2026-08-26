"""A re-registered AMI must not inherit a leaking root volume.

``ec2:ImportImage`` emits ``DeleteOnTermination: false`` on the root device, and
neither ``import_image`` nor ``copy_image`` accepts a block-device override — so the
re-register that renames a promoted AMI is the ONLY point in the whole promote where
that flag can be corrected. Left alone it strands an untagged root volume on every
terminate of a promoted image, which is exactly where this account's orphaned volumes
came from (docs/notes/aws-cost-guardrails.md).

The second half of the contract is the easy thing to get wrong: ``copy_image``
preserved boot attributes implicitly, ``register_image`` sets only what it is given.
Dropping ``BootMode`` from a UEFI image produces an AMI that registers fine, reports
"available", and then fails to boot — so the carry-forward list is asserted here
rather than trusted.

Runs under pytest, or standalone:
    python tests/test_ami_register_mappings.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from web_dashboard.services import aws_service  # noqa: E402


def _imported_ami(**over):
    """An AMI shaped like what ImportImage hands back: gp2 root, flag off."""
    img = {
        "Name": "import-ami-01d9b5d391f1ffedc",
        "RootDeviceName": "/dev/sda1",
        "Architecture": "x86_64",
        "VirtualizationType": "hvm",
        "BlockDeviceMappings": [{
            "DeviceName": "/dev/sda1",
            "Ebs": {"SnapshotId": "snap-0abc", "VolumeSize": 21,
                    "VolumeType": "gp2", "DeleteOnTermination": False},
        }],
    }
    img.update(over)
    return img


def _root(bdms, dev="/dev/sda1"):
    return next(m for m in bdms if m["DeviceName"] == dev)["Ebs"]


def test_the_imported_root_device_stops_outliving_its_instance():
    bdms = aws_service._ami_bdms_for_register(
        _imported_ami(), pin_delete_on_termination=True)
    assert _root(bdms)["DeleteOnTermination"] is True


def test_the_flag_is_left_alone_when_not_asked_for():
    """The ENA path and the promote path share this helper; only the promote pins."""
    bdms = aws_service._ami_bdms_for_register(_imported_ami())
    assert _root(bdms)["DeleteOnTermination"] is False


def test_pinning_does_not_touch_a_data_disk():
    """A data disk may be meant to outlive its instance. Only the root is pinned."""
    img = _imported_ami(BlockDeviceMappings=[
        {"DeviceName": "/dev/sda1",
         "Ebs": {"SnapshotId": "snap-root", "DeleteOnTermination": False}},
        {"DeviceName": "/dev/sdb",
         "Ebs": {"SnapshotId": "snap-data", "DeleteOnTermination": False}},
    ])
    bdms = aws_service._ami_bdms_for_register(img, pin_delete_on_termination=True)
    assert _root(bdms)["DeleteOnTermination"] is True
    assert _root(bdms, "/dev/sdb")["DeleteOnTermination"] is False


def test_the_snapshot_is_carried_over_so_the_image_still_has_its_bits():
    bdms = aws_service._ami_bdms_for_register(
        _imported_ami(), pin_delete_on_termination=True)
    assert _root(bdms)["SnapshotId"] == "snap-0abc"


def test_register_image_is_never_handed_a_field_it_rejects():
    """describe_images returns fields register_image refuses (Encrypted=False,
    KmsKeyId, VolumeInitializationRate...). Passing one through fails the call."""
    img = _imported_ami(BlockDeviceMappings=[{
        "DeviceName": "/dev/sda1",
        "Ebs": {"SnapshotId": "snap-0abc", "VolumeSize": 21, "VolumeType": "gp2",
                "DeleteOnTermination": False, "Encrypted": False,
                "KmsKeyId": "arn:aws:kms:...", "OutpostArn": "arn:aws:outposts:..."},
    }])
    ebs = _root(aws_service._ami_bdms_for_register(img, pin_delete_on_termination=True))
    assert set(ebs) <= {"SnapshotId", "VolumeSize", "VolumeType",
                        "DeleteOnTermination", "Encrypted"}
    # Encrypted=False is dropped rather than echoed back; only a true one is sent.
    assert "Encrypted" not in ebs
    assert "KmsKeyId" not in ebs


def test_an_encrypted_image_stays_encrypted():
    img = _imported_ami(BlockDeviceMappings=[{
        "DeviceName": "/dev/sda1",
        "Ebs": {"SnapshotId": "snap-0abc", "Encrypted": True,
                "DeleteOnTermination": False},
    }])
    assert _root(aws_service._ami_bdms_for_register(
        img, pin_delete_on_termination=True))["Encrypted"] is True


def test_boot_attributes_survive_the_re_register():
    """A UEFI image that loses BootMode registers fine and then will not boot."""
    img = _imported_ami(BootMode="uefi", TpmSupport="v2.0", EnaSupport=True,
                        SriovNetSupport="simple", ImdsSupport="v2.0")
    kwargs = aws_service._ami_register_kwargs(
        img, name="ot-sim-1", description="d",
        bdms=aws_service._ami_bdms_for_register(img, pin_delete_on_termination=True))
    assert kwargs["BootMode"] == "uefi"
    assert kwargs["TpmSupport"] == "v2.0"
    assert kwargs["EnaSupport"] is True
    assert kwargs["SriovNetSupport"] == "simple"
    assert kwargs["ImdsSupport"] == "v2.0"
    assert kwargs["Architecture"] == "x86_64"
    assert kwargs["VirtualizationType"] == "hvm"
    assert kwargs["RootDeviceName"] == "/dev/sda1"
    assert kwargs["Name"] == "ot-sim-1"


def test_absent_boot_attributes_are_omitted_not_sent_empty():
    """register_image rejects an empty BootMode; a plain BIOS image has none."""
    kwargs = aws_service._ami_register_kwargs(
        _imported_ami(), name="n", description="", bdms=[])
    for absent in ("BootMode", "TpmSupport", "EnaSupport", "SriovNetSupport",
                   "ImdsSupport", "KernelId", "RamdiskId", "Description"):
        assert absent not in kwargs, f"{absent} should be omitted when the source lacks it"


def test_the_carry_forward_list_holds_the_attributes_that_break_a_boot():
    """A guard on the list itself: these were dropped before this fix existed."""
    for key in ("BootMode", "TpmSupport", "EnaSupport", "SriovNetSupport",
                "Architecture", "VirtualizationType"):
        assert key in aws_service._AMI_CARRY_FORWARD


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failures = 0
    for fn in fns:
        try:
            fn()
            print(f"ok   {fn.__name__}")
        except Exception as e:  # noqa: BLE001
            failures += 1
            print(f"FAIL {fn.__name__}: {e}")
    print(f"\n{len(fns) - failures}/{len(fns)} passed")
    sys.exit(1 if failures else 0)
