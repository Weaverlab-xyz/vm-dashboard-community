--
-- PostgreSQL database dump
--



SET statement_timeout = 0;
SET lock_timeout = 0;
SET idle_in_transaction_session_timeout = 0;
SET client_encoding = 'UTF8';
SET standard_conforming_strings = on;
SELECT pg_catalog.set_config('search_path', '', false);
SET check_function_bodies = false;
SET xmloption = content;
SET client_min_messages = warning;
SET row_security = off;

SET default_tablespace = '';

SET default_table_access_method = heap;

--
-- Name: access_roles; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.access_roles (
    id character varying(36) NOT NULL,
    slug character varying(64) NOT NULL,
    name character varying(200) NOT NULL,
    description text,
    permissions text,
    is_builtin boolean NOT NULL,
    created_at timestamp without time zone NOT NULL,
    updated_at timestamp without time zone,
    created_by_user_id character varying(36)
);


--
-- Name: agent_cells; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.agent_cells (
    id character varying(36) NOT NULL,
    name character varying(120) NOT NULL,
    status character varying(32) NOT NULL,
    created_at timestamp without time zone NOT NULL,
    created_by character varying(100),
    workgroup character varying(100),
    host_deploy_job_id character varying(36),
    host_name character varying(120),
    cloud character varying(20),
    private_ip character varying(64),
    public_ip character varying(64),
    spire_lab_id character varying(36),
    trust_domain character varying(255),
    spiffe_id character varying(500),
    pat_id character varying(36),
    pat_name character varying(100),
    pat_user_id character varying(36),
    pat_expires_at timestamp without time zone,
    pat_revoked_at timestamp without time zone,
    oauth_client_id character varying(36),
    linked_mechanism character varying(32),
    linked_credential_id character varying(36),
    linked_at timestamp without time zone,
    episode_state character varying(20),
    episode_started_at timestamp without time zone,
    episode_released_at timestamp without time zone,
    stages_done text,
    stage_job_ids text,
    error_message text
);


--
-- Name: agent_enroll_attempts; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.agent_enroll_attempts (
    id integer NOT NULL,
    ip character varying(45) NOT NULL,
    attempted_at timestamp without time zone NOT NULL
);


--
-- Name: agent_enroll_attempts_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.agent_enroll_attempts_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: agent_enroll_attempts_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.agent_enroll_attempts_id_seq OWNED BY public.agent_enroll_attempts.id;


--
-- Name: agent_hosts; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.agent_hosts (
    id character varying(36) NOT NULL,
    agent_id character varying(36) NOT NULL,
    name character varying(63) NOT NULL,
    cloud character varying(20) NOT NULL,
    region character varying(40) NOT NULL,
    zone character varying(40),
    network character varying(200),
    subnet character varying(400),
    instance_size character varying(64),
    ansible_cidrs text NOT NULL,
    max_concurrent integer NOT NULL,
    k8s_enabled boolean,
    mode character varying(16) NOT NULL,
    until_at timestamp without time zone,
    change_window_id character varying(36),
    lead_minutes integer NOT NULL,
    grace_minutes integer NOT NULL,
    status character varying(32) NOT NULL,
    host_id character varying(200),
    last_job_id character varying(36),
    error text,
    next_start_at timestamp without time zone,
    next_stop_at timestamp without time zone,
    started_at timestamp without time zone,
    created_by character varying(100),
    created_at timestamp without time zone NOT NULL
);


--
-- Name: agent_nonces; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.agent_nonces (
    id integer NOT NULL,
    agent_id character varying(36) NOT NULL,
    nonce character varying(64) NOT NULL,
    seen_at timestamp without time zone NOT NULL
);


--
-- Name: agent_nonces_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.agent_nonces_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: agent_nonces_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.agent_nonces_id_seq OWNED BY public.agent_nonces.id;


--
-- Name: agent_releases; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.agent_releases (
    id character varying(36) NOT NULL,
    version character varying(32) NOT NULL,
    repository character varying(200) NOT NULL,
    digest character varying(71) NOT NULL,
    notes text,
    source character varying(16) NOT NULL,
    created_at timestamp without time zone NOT NULL,
    created_by character varying(100)
);


--
-- Name: agent_rollouts; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.agent_rollouts (
    id character varying(36) NOT NULL,
    release_id character varying(36) NOT NULL,
    change_window_id character varying(36),
    status character varying(16) NOT NULL,
    canary_job_ids text,
    default_job_ids text,
    skipped text,
    detail text,
    created_at timestamp without time zone NOT NULL,
    created_by character varying(100),
    finished_at timestamp without time zone
);


--
-- Name: app_config; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.app_config (
    key character varying(128) NOT NULL,
    value text,
    workgroup character varying(64),
    updated_at timestamp without time zone NOT NULL
);


--
-- Name: approvals; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.approvals (
    id character varying(36) NOT NULL,
    entitle_request_id character varying(255) NOT NULL,
    action character varying(100) NOT NULL,
    user_id character varying(36) NOT NULL,
    payload_hash character varying(64) NOT NULL,
    status character varying(20) NOT NULL,
    requested_at timestamp without time zone NOT NULL,
    approved_at timestamp without time zone,
    expires_at timestamp without time zone NOT NULL,
    consumed_at timestamp without time zone,
    denial_reason text,
    principal_kind character varying(16) NOT NULL
);


--
-- Name: audit_log; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.audit_log (
    id character varying(36) NOT NULL,
    "timestamp" timestamp without time zone NOT NULL,
    username character varying(100),
    action character varying(100) NOT NULL,
    target_vm text,
    details text,
    ip_address character varying(45),
    seq integer,
    prev_hash character varying(64),
    entry_hash character varying(64)
);


--
-- Name: beyondtrust_tenants; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.beyondtrust_tenants (
    id character varying(36) NOT NULL,
    kind character varying(16) NOT NULL,
    name character varying(64) NOT NULL,
    base_url character varying(255) NOT NULL,
    client_id character varying(255),
    secret_enc text,
    secret_ref character varying(256),
    options text,
    is_default boolean NOT NULL,
    is_active boolean NOT NULL,
    last_checked_at timestamp without time zone,
    last_ok_at timestamp without time zone,
    last_error text,
    created_at timestamp without time zone NOT NULL,
    created_by character varying(100),
    updated_at timestamp without time zone
);


--
-- Name: brand_asset; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.brand_asset (
    slot character varying(32) NOT NULL,
    content_type character varying(64) NOT NULL,
    data bytea NOT NULL,
    sha256 character varying(64) NOT NULL,
    byte_size integer NOT NULL,
    width integer,
    height integer,
    filename character varying(255),
    uploaded_by character varying(128),
    updated_at timestamp without time zone NOT NULL
);


--
-- Name: cert_labs; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.cert_labs (
    id character varying(36) NOT NULL,
    name character varying(120) NOT NULL,
    cloud character varying(20) NOT NULL,
    backend character varying(20) NOT NULL,
    project character varying(120),
    location character varying(64),
    pool_id character varying(120),
    ca_arn character varying(255),
    ca_path_length integer NOT NULL,
    status character varying(32) NOT NULL,
    deploy_job_id character varying(36),
    ca_chain_pem text,
    enroll_account character varying(255),
    ps_functional_account character varying(255),
    ps_functional_account_id character varying(36),
    ps_subca_functional_account character varying(255),
    ps_subca_functional_account_id character varying(36),
    ps_system_id character varying(36),
    ps_account_id character varying(36),
    ps_address text,
    ps_package character varying(16),
    ps_tf_state text,
    error_message text,
    workgroup character varying(100),
    created_by character varying(100),
    created_at timestamp without time zone NOT NULL,
    updated_at timestamp without time zone,
    expires_at timestamp without time zone,
    expiry_warned_at timestamp without time zone
);


--
-- Name: change_windows; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.change_windows (
    id character varying(36) NOT NULL,
    name character varying(120) NOT NULL,
    description text,
    start_at_local character varying(5) NOT NULL,
    duration_minutes integer NOT NULL,
    timezone character varying(64),
    schedule_days character varying(7) NOT NULL,
    enabled boolean,
    created_by character varying(100),
    created_at timestamp without time zone NOT NULL
);


--
-- Name: cloud_cost_cache; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.cloud_cost_cache (
    cloud character varying(16) NOT NULL,
    view character varying(16) NOT NULL,
    payload text,
    payload_version integer NOT NULL,
    period character varying(7),
    fetched_at timestamp without time zone,
    stale boolean NOT NULL,
    last_attempt_at timestamp without time zone,
    last_error text,
    consecutive_failures integer NOT NULL,
    cooldown_until timestamp without time zone,
    lease_until timestamp without time zone,
    lease_owner character varying(64),
    next_query_allowed_at timestamp without time zone,
    updated_at timestamp without time zone NOT NULL
);


--
-- Name: cloud_databases; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.cloud_databases (
    id character varying(36) NOT NULL,
    engine character varying(20) NOT NULL,
    provider character varying(40),
    cloud character varying(20) NOT NULL,
    region character varying(64),
    source character varying(16) NOT NULL,
    instance_id character varying(255),
    private_host character varying(255),
    port integer,
    db_name character varying(128),
    status character varying(32) NOT NULL,
    credentials_ref text,
    jump_item_id character varying(64),
    jump_group character varying(128),
    jumpoint_name character varying(128),
    pra_credential_ref character varying(256),
    entitle_integration_id character varying(64),
    ps_managed_system_id character varying(64),
    ps_managed_account_id character varying(64),
    agent_id character varying(36),
    route_cidrs text,
    route_network character varying(200),
    deploy_workgroup character varying(100),
    workgroup character varying(100),
    created_by character varying(100),
    created_at timestamp without time zone NOT NULL,
    expires_at timestamp without time zone,
    expiry_warned_at timestamp without time zone
);


--
-- Name: cloud_functions; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.cloud_functions (
    id character varying(36) NOT NULL,
    name character varying(200) NOT NULL,
    workload character varying(64) NOT NULL,
    cloud character varying(20) NOT NULL,
    region character varying(64),
    provider character varying(40),
    runtime character varying(20),
    status character varying(32) NOT NULL,
    resource_id character varying(255),
    invoke_url character varying(500),
    package_sha256 character varying(64),
    package_uri character varying(500),
    auth_mode character varying(32),
    invoke_secret_ref text,
    invoke_key_ref text,
    network_mode character varying(16) NOT NULL,
    network_ref text,
    env_ref text,
    provenance text,
    deploy_job_id character varying(36),
    entitle_integration_id character varying(64),
    created_by character varying(100),
    created_at timestamp without time zone NOT NULL,
    updated_at timestamp without time zone
);


--
-- Name: config_apply_state; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.config_apply_state (
    id integer NOT NULL,
    target character varying(255) NOT NULL,
    playbook_ref character varying(255) NOT NULL,
    content_hash character varying(64) NOT NULL,
    inputs_hash character varying(64),
    applied_at timestamp without time zone NOT NULL,
    job_id character varying(36)
);


--
-- Name: config_apply_state_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.config_apply_state_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: config_apply_state_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.config_apply_state_id_seq OWNED BY public.config_apply_state.id;


--
-- Name: config_mgmt_routes; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.config_mgmt_routes (
    id character varying(36) NOT NULL,
    agent_id character varying(36) NOT NULL,
    cidr character varying(64) NOT NULL,
    label character varying(64),
    is_active boolean NOT NULL,
    include_cloud_vms boolean,
    scope character varying(16) DEFAULT 'onprem'::character varying NOT NULL,
    scope_region character varying(64) DEFAULT ''::character varying NOT NULL,
    scope_connection_id character varying(36) DEFAULT ''::character varying NOT NULL,
    scope_workstream character varying(100) DEFAULT ''::character varying NOT NULL,
    scope_network character varying(200) DEFAULT ''::character varying NOT NULL,
    fallback_to_worker boolean,
    created_at timestamp without time zone NOT NULL,
    created_by character varying(100),
    updated_at timestamp without time zone
);


--
-- Name: container_state_cache; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.container_state_cache (
    container_id character varying(64) NOT NULL,
    short_id character varying(12),
    name character varying(200),
    image character varying(500),
    state character varying(50),
    status character varying(200),
    ports text,
    endpoint_id integer,
    endpoint_name character varying(200),
    workgroup character varying(50),
    created_ts integer,
    last_updated timestamp without time zone NOT NULL
);


--
-- Name: dashboard_stat_cache; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.dashboard_stat_cache (
    tile_key character varying(48) NOT NULL,
    scope character varying(64) NOT NULL,
    provider character varying(24) NOT NULL,
    payload text,
    payload_version integer NOT NULL,
    fetched_at timestamp without time zone,
    stale boolean NOT NULL,
    last_attempt_at timestamp without time zone,
    last_error text,
    consecutive_failures integer NOT NULL,
    cooldown_until timestamp without time zone,
    lease_until timestamp without time zone,
    lease_owner character varying(64),
    next_query_allowed_at timestamp without time zone,
    updated_at timestamp without time zone NOT NULL
);


--
-- Name: directory_health; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.directory_health (
    directory_id character varying(36) NOT NULL,
    status character varying(16) NOT NULL,
    checks text,
    checked_at timestamp without time zone,
    duration_ms integer,
    pending_job_id character varying(36),
    pending_since timestamp without time zone,
    probe_queued_at timestamp without time zone,
    deep_job_id character varying(36),
    deep_since timestamp without time zone,
    deep_queued_at timestamp without time zone
);


--
-- Name: entitle_activations; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.entitle_activations (
    id character varying(36) NOT NULL,
    cloud character varying(16) NOT NULL,
    operation character varying(64) NOT NULL,
    role character varying(255),
    requester_user_id character varying(36),
    entitle_request_id character varying(255),
    entitle_policy_id character varying(255),
    auto_approved boolean NOT NULL,
    status character varying(20) NOT NULL,
    denial_reason text,
    payload_hash character varying(64) NOT NULL,
    tenant_id character varying(64),
    requested_at timestamp without time zone NOT NULL,
    granted_at timestamp without time zone,
    expires_at timestamp without time zone,
    revoked_at timestamp without time zone
);


--
-- Name: entitle_bundle_sets; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.entitle_bundle_sets (
    id character varying(36) NOT NULL,
    status character varying(16) NOT NULL,
    bundles_json text NOT NULL,
    tf_state_json text,
    error text,
    created_by character varying(255),
    created_at timestamp without time zone,
    updated_at timestamp without time zone
);


--
-- Name: entitle_virtual_apps; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.entitle_virtual_apps (
    id character varying(36) NOT NULL,
    directory_id character varying(36) NOT NULL,
    name character varying(50) NOT NULL,
    status character varying(16) NOT NULL,
    integration_id character varying(64),
    roles_json text NOT NULL,
    tf_state_json text,
    error text,
    created_by character varying(255),
    created_at timestamp without time zone,
    updated_at timestamp without time zone
);


--
-- Name: ephemeral_state; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.ephemeral_state (
    key character varying(200) NOT NULL,
    value text NOT NULL,
    expires_at timestamp without time zone NOT NULL
);


--
-- Name: external_workload_identities; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.external_workload_identities (
    id character varying(36) NOT NULL,
    user_id character varying(36) NOT NULL,
    issuer character varying(500) NOT NULL,
    subject character varying(255) NOT NULL,
    expected_client character varying(255),
    name character varying(100) NOT NULL,
    created_at timestamp without time zone NOT NULL,
    created_by character varying(100),
    last_used_at timestamp without time zone,
    is_active boolean
);


--
-- Name: fido2_credentials; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.fido2_credentials (
    id character varying(36) NOT NULL,
    user_id character varying(36) NOT NULL,
    credential_id bytea NOT NULL,
    public_key bytea NOT NULL,
    sign_count integer NOT NULL,
    aaguid character varying(36),
    device_name character varying(100),
    created_at timestamp without time zone NOT NULL,
    last_used_at timestamp without time zone,
    is_active boolean
);


--
-- Name: gateways; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.gateways (
    id character varying(36) NOT NULL,
    cloud character varying(20) NOT NULL,
    region character varying(40),
    zone character varying(40),
    name character varying(200) NOT NULL,
    status character varying(32) NOT NULL,
    managed boolean NOT NULL,
    host_id character varying(128),
    egress_ip character varying(45),
    deploy_job_id character varying(36),
    error text,
    created_by character varying(100),
    created_at timestamp without time zone NOT NULL
);


--
-- Name: health_areas; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.health_areas (
    area character varying(32) NOT NULL,
    last_run_at timestamp without time zone,
    last_ok_at timestamp without time zone,
    last_error text,
    duration_ms integer
);


--
-- Name: health_checks; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.health_checks (
    area character varying(32) NOT NULL,
    object_id character varying(160) NOT NULL,
    name character varying(255) NOT NULL,
    kind character varying(64),
    href character varying(255),
    status character varying(16) NOT NULL,
    checks text,
    checked_at timestamp without time zone
);


--
-- Name: hypervisor_connections; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.hypervisor_connections (
    id character varying(36) NOT NULL,
    kind character varying(16) NOT NULL,
    name character varying(64) NOT NULL,
    host character varying(255) NOT NULL,
    port integer,
    username character varying(255),
    secret_enc text,
    secret_ref character varying(256),
    verify_ssl boolean NOT NULL,
    options text,
    agent_id character varying(36),
    agent_connection_name character varying(64),
    site character varying(64),
    is_default boolean NOT NULL,
    is_active boolean NOT NULL,
    last_ok_at timestamp without time zone,
    last_error text,
    last_sync_at timestamp without time zone,
    created_at timestamp without time zone NOT NULL,
    created_by character varying(100),
    updated_at timestamp without time zone
);


--
-- Name: hypervisor_vm_cache; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.hypervisor_vm_cache (
    connection_id character varying(36) NOT NULL,
    vm_id character varying(128) NOT NULL,
    name character varying(256),
    power_state character varying(32),
    vcpus integer,
    mem_mib integer,
    ip_addresses text,
    scope text,
    vm_type character varying(16),
    tags text,
    guest_os character varying(64),
    synced_at timestamp without time zone NOT NULL
);


--
-- Name: job_logs; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.job_logs (
    id integer NOT NULL,
    job_id character varying(36) NOT NULL,
    seq integer NOT NULL,
    line text NOT NULL,
    created_at timestamp without time zone NOT NULL
);


--
-- Name: job_logs_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.job_logs_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: job_logs_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.job_logs_id_seq OWNED BY public.job_logs.id;


--
-- Name: job_schedules; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.job_schedules (
    id character varying(36) NOT NULL,
    name character varying(160) NOT NULL,
    change_window_id character varying(36),
    job_type character varying(50) NOT NULL,
    payload text,
    workgroup character varying(50),
    agent_id character varying(36),
    vm_path text,
    approval_required boolean,
    created_by character varying(100),
    created_at timestamp without time zone NOT NULL,
    enabled boolean,
    last_materialised_for timestamp without time zone,
    last_run_at timestamp without time zone,
    last_job_id character varying(36),
    consecutive_failures integer NOT NULL,
    disabled_reason text
);


--
-- Name: jobs; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.jobs (
    id character varying(36) NOT NULL,
    job_type character varying(50) NOT NULL,
    workgroup character varying(50),
    vm_path text,
    cloud_resource_id character varying(255),
    batch_id character varying(32),
    agent_id character varying(36),
    is_checkin boolean,
    expires_at timestamp without time zone,
    expiry_warned_at timestamp without time zone,
    suspend_at_local character varying(5),
    resume_at_local character varying(5),
    schedule_timezone character varying(64),
    schedule_days character varying(7),
    schedule_last_checked_at timestamp without time zone,
    spend_cap_usd double precision,
    spend_estimate_usd double precision,
    spend_accrued_at timestamp without time zone,
    spend_warned_at timestamp without time zone,
    spend_capped_at timestamp without time zone,
    attempts integer NOT NULL,
    retry_after timestamp without time zone,
    scheduled_for timestamp without time zone,
    window_ends_at timestamp without time zone,
    change_window_id character varying(36),
    job_schedule_id character varying(36),
    approval_required boolean,
    approved_at timestamp without time zone,
    approved_by character varying(100),
    missed_window_at timestamp without time zone,
    status character varying(20) NOT NULL,
    progress_pct integer,
    progress_message text,
    created_at timestamp without time zone NOT NULL,
    started_at timestamp without time zone,
    completed_at timestamp without time zone,
    updated_at timestamp without time zone,
    created_by character varying(100),
    error_message text,
    extra_data text
);


--
-- Name: k8s_clusters; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.k8s_clusters (
    id character varying(36) NOT NULL,
    cloud character varying(20) NOT NULL,
    name character varying(200) NOT NULL,
    status character varying(32) NOT NULL,
    source character varying(16) NOT NULL,
    region character varying(40),
    api_server character varying(255),
    kubeconfig_ref text,
    deploy_job_id character varying(36),
    egress_ip character varying(45),
    mgmt_kind character varying(20),
    mgmt_endpoint character varying(255),
    pra_jump_id character varying(64),
    pra_tunnel_state text,
    jump_group character varying(128),
    jumpoint_name character varying(128),
    pra_credential_ref character varying(256),
    secrets_delivery_kind character varying(20),
    ps_token_account_id character varying(64),
    ps_pra_vault_account_id character varying(64),
    pra_vault_account_id character varying(64),
    workgroup character varying(100),
    executor_agent_id character varying(36),
    created_by character varying(100),
    created_at timestamp without time zone NOT NULL,
    expires_at timestamp without time zone,
    expiry_warned_at timestamp without time zone
);


--
-- Name: login_attempts; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.login_attempts (
    id integer NOT NULL,
    username character varying(150) NOT NULL,
    ip character varying(45) NOT NULL,
    attempted_at timestamp without time zone NOT NULL
);


--
-- Name: login_attempts_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.login_attempts_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: login_attempts_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.login_attempts_id_seq OWNED BY public.login_attempts.id;


--
-- Name: managed_directories; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.managed_directories (
    id character varying(36) NOT NULL,
    name character varying(255) NOT NULL,
    netbios character varying(15),
    cloud character varying(20) NOT NULL,
    provider character varying(32) NOT NULL,
    source character varying(16) NOT NULL,
    status character varying(32) NOT NULL,
    edition character varying(32),
    region character varying(64),
    locations text,
    project character varying(120),
    directory_id character varying(64),
    resource_name character varying(255),
    vpc_id character varying(64),
    subnet_ids text,
    networks text,
    reserved_ip_range character varying(32),
    dns_ips text,
    security_group_id character varying(64),
    agent_id character varying(36),
    host character varying(255),
    port integer,
    use_ldaps boolean,
    base_dn character varying(255),
    credentials_ref text,
    linked_directory_id character varying(36),
    link_config text,
    endpoint character varying(255),
    tenant_id character varying(64),
    client_id character varying(255),
    auth_mode character varying(32),
    writes_enabled boolean,
    options text,
    vendor character varying(32),
    entitle_integration_id character varying(64),
    entitle_integration_name character varying(255),
    admin_username character varying(64),
    admin_password_backend character varying(32),
    admin_password_ref character varying(255),
    admin_password_custody character varying(32),
    ps_system_id character varying(36),
    ps_account_id character varying(36),
    ps_tf_state text,
    ps_error text,
    deploy_job_id character varying(36),
    error_message text,
    workgroup character varying(100),
    created_by character varying(100),
    created_at timestamp without time zone NOT NULL,
    updated_at timestamp without time zone,
    expires_at timestamp without time zone,
    expiry_warned_at timestamp without time zone
);


--
-- Name: notification_deliveries; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.notification_deliveries (
    id character varying(36) NOT NULL,
    created_at timestamp without time zone NOT NULL,
    event_type character varying(64) NOT NULL,
    severity character varying(16) NOT NULL,
    resource_id character varying(128),
    resource_kind character varying(24),
    resource_name character varying(255),
    cloud character varying(24),
    region character varying(40),
    workgroup character varying(64),
    url text,
    endpoint_id character varying(36),
    channel character varying(32),
    reason text,
    subject text,
    body text,
    payload text,
    status character varying(16) NOT NULL,
    attempts integer NOT NULL,
    next_attempt_at timestamp without time zone,
    claimed_at timestamp without time zone,
    sent_at timestamp without time zone,
    error text,
    dedupe_key character varying(200) NOT NULL
);


--
-- Name: notification_endpoints; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.notification_endpoints (
    id character varying(36) NOT NULL,
    name character varying(100) NOT NULL,
    url text NOT NULL,
    fmt character varying(16) NOT NULL,
    secret text,
    enabled boolean NOT NULL,
    event_types text,
    created_at timestamp without time zone NOT NULL,
    created_by character varying(100),
    last_success_at timestamp without time zone,
    last_error text
);


--
-- Name: oauth_clients; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.oauth_clients (
    id character varying(36) NOT NULL,
    client_id character varying(64) NOT NULL,
    user_id character varying(36) NOT NULL,
    name character varying(100) NOT NULL,
    secret_hash character varying(64) NOT NULL,
    secret_expires_at timestamp without time zone,
    previous_secret_hash character varying(64),
    previous_expires_at timestamp without time zone,
    token_ttl_seconds integer NOT NULL,
    created_at timestamp without time zone NOT NULL,
    created_by character varying(100),
    last_used_at timestamp without time zone,
    is_active boolean,
    auth_method character varying(20),
    spiffe_id character varying(500)
);


--
-- Name: oauth_group_mappings; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.oauth_group_mappings (
    id character varying(36) NOT NULL,
    entra_group_id character varying(255) NOT NULL,
    display_name character varying(200) NOT NULL,
    workgroup character varying(100) NOT NULL,
    default_permissions text,
    persona character varying(32),
    persona_priority integer,
    role_id character varying(36),
    created_at timestamp without time zone NOT NULL
);


--
-- Name: personal_access_tokens; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.personal_access_tokens (
    id character varying(36) NOT NULL,
    user_id character varying(36) NOT NULL,
    name character varying(100) NOT NULL,
    token_hash character varying(64) NOT NULL,
    created_at timestamp without time zone NOT NULL,
    expires_at timestamp without time zone,
    last_used_at timestamp without time zone,
    is_active boolean
);


--
-- Name: pov_accessors; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.pov_accessors (
    id character varying(36) NOT NULL,
    environment_id character varying(36) NOT NULL,
    user_id character varying(36),
    username character varying(100) NOT NULL,
    email character varying(200),
    full_name character varying(200),
    source character varying(16) NOT NULL,
    entitle_request_id character varying(64),
    entitle_actor_id character varying(128),
    expires_at timestamp without time zone,
    created_at timestamp without time zone NOT NULL,
    created_by character varying(100),
    revoked_at timestamp without time zone,
    revoke_reason character varying(255),
    last_login_at timestamp without time zone
);


--
-- Name: pov_blueprints; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.pov_blueprints (
    id character varying(36) NOT NULL,
    name character varying(255) NOT NULL,
    description text,
    platform character varying(32) NOT NULL,
    template_id character varying(64) NOT NULL,
    template_name character varying(255),
    project_id character varying(64),
    source_build_id character varying(36),
    broker_vm_name character varying(255),
    suspend_on_idle_seconds integer,
    suspend_at_local character varying(5),
    resume_at_local character varying(5),
    schedule_timezone character varying(64),
    schedule_days character varying(7),
    spend_cap_usd double precision,
    pra_tenant_id character varying(36),
    ps_tenant_id character varying(36),
    entitle_tenant_id character varying(36),
    use_case_products character varying(64),
    use_case_excluded text,
    gateway_name character varying(255),
    rb_windows_vm_name character varying(255),
    rb_zone character varying(255),
    rb_asset character varying(255),
    expiry_hours integer,
    workgroup character varying(100),
    created_by character varying(100),
    created_at timestamp without time zone NOT NULL,
    updated_at timestamp without time zone NOT NULL
);


--
-- Name: pov_cloud_template_vms; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.pov_cloud_template_vms (
    id character varying(36) NOT NULL,
    template_id character varying(36) NOT NULL,
    name character varying(255) NOT NULL,
    role character varying(16) NOT NULL,
    cell_role character varying(32),
    os_family character varying(16) NOT NULL,
    image_ref character varying(36),
    image_id character varying(255),
    instance_type character varying(64) NOT NULL,
    disk_gb integer,
    sort_order integer NOT NULL,
    created_at timestamp without time zone NOT NULL
);


--
-- Name: pov_cloud_templates; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.pov_cloud_templates (
    id character varying(36) NOT NULL,
    cloud character varying(16) NOT NULL,
    name character varying(255) NOT NULL,
    description text,
    region character varying(64),
    network_cidr character varying(64),
    purdue_zones boolean,
    source_environment_id character varying(36),
    workgroup character varying(100),
    created_by character varying(100),
    created_at timestamp without time zone NOT NULL,
    updated_at timestamp without time zone NOT NULL
);


--
-- Name: pov_environment_vms; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.pov_environment_vms (
    id character varying(36) NOT NULL,
    environment_id character varying(36) NOT NULL,
    platform_vm_id character varying(64) NOT NULL,
    name character varying(255),
    os_family character varying(16),
    runstate character varying(32),
    private_ip character varying(64),
    published_services text,
    pra_jump_id character varying(64),
    pra_jump_tf_state text,
    vault_account_id character varying(64),
    vault_tf_state text,
    ps_managed_system_id character varying(64),
    ps_managed_account_id character varying(64),
    ps_registration_tf_state text,
    entitle_integration_id character varying(64),
    entitle_tf_state text,
    wiring_error text,
    login_username character varying(104),
    os_family_override character varying(16),
    cell_role character varying(32),
    cell_artifacts text,
    created_at timestamp without time zone NOT NULL
);


--
-- Name: pov_environments; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.pov_environments (
    id character varying(36) NOT NULL,
    platform character varying(32) NOT NULL,
    name character varying(255) NOT NULL,
    platform_environment_id character varying(64),
    template_id character varying(64),
    template_name character varying(255),
    region character varying(64),
    project_id character varying(64),
    status character varying(32) NOT NULL,
    runstate character varying(32),
    workgroup character varying(100),
    created_by character varying(100),
    created_at timestamp without time zone NOT NULL,
    provision_job_id character varying(36),
    error_message text,
    broker_vm_id character varying(64),
    broker_agent_id character varying(36),
    gateway_name character varying(255),
    pra_require_totp boolean,
    pra_gateway_id character varying(64),
    ps_application_host_id integer,
    pra_ps_matched_count integer,
    pra_ps_linked_count integer,
    pra_ps_checked_at timestamp without time zone,
    pra_tenant_id character varying(36),
    ps_tenant_id character varying(36),
    entitle_tenant_id character varying(36),
    use_case_products character varying(64),
    use_case_excluded text,
    accessor_integration_id character varying(64),
    accessor_tf_state text,
    pra_jump_group_name character varying(255),
    pra_jump_group_id character varying(36),
    pra_vendor_policy_id character varying(36),
    pra_vendor_group_id character varying(36),
    pra_vendor_expires_at timestamp without time zone,
    share_url text,
    share_id character varying(64),
    share_expires_at timestamp without time zone,
    platform_seen_at timestamp without time zone,
    rate_limited boolean,
    platform_missing boolean,
    suspend_on_idle_seconds integer,
    suspend_at_local character varying(5),
    resume_at_local character varying(5),
    schedule_timezone character varying(64),
    schedule_days character varying(7),
    schedule_last_checked_at timestamp without time zone,
    wake_at timestamp without time zone,
    spend_cap_usd double precision,
    spend_estimate_usd double precision,
    spend_accrued_at timestamp without time zone,
    spend_warned_at timestamp without time zone,
    spend_capped_at timestamp without time zone,
    ps_linux_functional_account_id integer,
    ps_windows_functional_account_id integer,
    expires_at timestamp without time zone,
    expiry_warned_at timestamp without time zone,
    warned_stage_minutes integer,
    extra_data text
);


--
-- Name: pov_platform_cache; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.pov_platform_cache (
    kind character varying(24) NOT NULL,
    platform character varying(24) NOT NULL,
    project_id character varying(64) NOT NULL,
    payload text,
    payload_version integer NOT NULL,
    fetched_at timestamp without time zone,
    stale boolean NOT NULL,
    last_attempt_at timestamp without time zone,
    last_error text,
    consecutive_failures integer NOT NULL,
    cooldown_until timestamp without time zone,
    lease_until timestamp without time zone,
    lease_owner character varying(64),
    next_query_allowed_at timestamp without time zone,
    updated_at timestamp without time zone NOT NULL
);


--
-- Name: pov_signoffs; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.pov_signoffs (
    id character varying(36) NOT NULL,
    environment_id character varying(36) NOT NULL,
    signed_by character varying(100) NOT NULL,
    signed_by_kind character varying(16) NOT NULL,
    signed_at timestamp without time zone NOT NULL,
    coverage_json text NOT NULL,
    summary_hash character varying(64) NOT NULL,
    revoked_at timestamp without time zone,
    revoked_by character varying(100)
);


--
-- Name: pov_template_builds; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.pov_template_builds (
    id character varying(36) NOT NULL,
    platform character varying(32) NOT NULL,
    name character varying(255) NOT NULL,
    description text,
    base_template_id character varying(64),
    base_template_name character varying(255),
    project_id character varying(64),
    build_environment_id character varying(64),
    build_environment_was character varying(64),
    broker_vm_name character varying(255),
    broker_vm_id character varying(64),
    result_template_id character varying(64),
    result_template_name character varying(255),
    status character varying(32) NOT NULL,
    contract_report text,
    prepare_method character varying(16),
    prepare_detail text,
    error_message text,
    job_id character varying(36),
    keep_build_environment boolean NOT NULL,
    workgroup character varying(100),
    created_by character varying(100),
    created_at timestamp without time zone NOT NULL
);


--
-- Name: pov_use_case_progress; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.pov_use_case_progress (
    id character varying(36) NOT NULL,
    environment_id character varying(36) NOT NULL,
    card_id character varying(64) NOT NULL,
    group_key character varying(32) NOT NULL,
    state character varying(16) NOT NULL,
    note text,
    checked_by character varying(100),
    checked_by_kind character varying(16) NOT NULL,
    checked_at timestamp without time zone NOT NULL
);


--
-- Name: pov_vendor_users; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.pov_vendor_users (
    id character varying(36) NOT NULL,
    environment_id character varying(36) NOT NULL,
    pra_vendor_user_id character varying(36),
    username character varying(64) NOT NULL,
    email character varying(256),
    full_name character varying(200),
    expires_at timestamp without time zone,
    created_at timestamp without time zone NOT NULL,
    created_by character varying(100),
    revoked_at timestamp without time zone,
    revoke_reason character varying(255)
);


--
-- Name: registered_images; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.registered_images (
    id character varying(36) NOT NULL,
    name character varying(200) NOT NULL,
    version character varying(64) NOT NULL,
    description text,
    source_cloud character varying(20) NOT NULL,
    source_image_id character varying(500),
    source_region character varying(64),
    artefact_url character varying(1000),
    artefact_format character varying(20),
    os_type character varying(20),
    recipe character varying(40),
    promotions text,
    created_at timestamp without time zone NOT NULL,
    created_by character varying(100) NOT NULL
);


--
-- Name: remote_agents; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.remote_agents (
    id character varying(36) NOT NULL,
    name character varying(64) NOT NULL,
    site character varying(64),
    description text,
    enroll_code_hash character varying(64),
    enroll_expires_at timestamp without time zone,
    public_key character varying(64),
    policy_hash character varying(64),
    allowed_job_types text,
    agent_version character varying(32),
    reported_job_types text,
    reported_ansible_slots integer,
    enrolled_at timestamp without time zone,
    last_seen_at timestamp without time zone,
    last_seen_ip character varying(45),
    is_active boolean NOT NULL,
    created_at timestamp without time zone NOT NULL,
    created_by character varying(100),
    spiffe_id character varying(255),
    auth_mode character varying(16),
    control_upgrades boolean,
    control_updater boolean,
    control_k8s_signature character varying(240),
    paused boolean,
    upgrade_ring character varying(16),
    last_attest_at timestamp without time zone,
    last_attest_ip character varying(45),
    previous_public_key character varying(64),
    previous_key_until timestamp without time zone,
    attention text,
    reported_reattest_hours integer
);


--
-- Name: runner_credential_grants; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.runner_credential_grants (
    id character varying(36) NOT NULL,
    token_hash character varying(64) NOT NULL,
    job_id character varying(36) NOT NULL,
    runner character varying(16) NOT NULL,
    values_enc text NOT NULL,
    expires_at timestamp without time zone NOT NULL,
    redeemed_at timestamp without time zone,
    created_at timestamp without time zone NOT NULL
);


--
-- Name: schema_markers; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.schema_markers (
    key character varying(128) NOT NULL,
    applied_at timestamp without time zone NOT NULL,
    detail text
);


--
-- Name: secret_vaults; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.secret_vaults (
    id character varying(64) NOT NULL,
    backend character varying(32) NOT NULL,
    endpoint text NOT NULL,
    credentials_ref text,
    workgroup character varying(64),
    is_default boolean NOT NULL,
    created_at timestamp without time zone NOT NULL,
    created_by_user_id character varying(36)
);


--
-- Name: spiffe_trust_domains; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.spiffe_trust_domains (
    id character varying(36) NOT NULL,
    trust_domain character varying(255) NOT NULL,
    jwks_url character varying(500),
    ca_pem text,
    tls_server_name character varying(255),
    bundle_json text,
    bundle_captured_at timestamp without time zone,
    spire_lab_id character varying(36),
    created_at timestamp without time zone NOT NULL,
    created_by character varying(100),
    updated_at timestamp without time zone
);


--
-- Name: spire_admin_calls; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.spire_admin_calls (
    id character varying(36) NOT NULL,
    args_json text NOT NULL,
    stdin_enc text,
    timeout_s integer NOT NULL,
    status character varying(16) NOT NULL,
    returncode integer,
    stdout_enc text,
    stderr_enc text,
    requested_by character varying(255),
    served_by character varying(255),
    created_at timestamp without time zone NOT NULL,
    claimed_at timestamp without time zone,
    finished_at timestamp without time zone
);


--
-- Name: spire_admin_relays; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.spire_admin_relays (
    id character varying(255) NOT NULL,
    transport character varying(16) NOT NULL,
    seen_at timestamp without time zone NOT NULL
);


--
-- Name: spire_labs; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.spire_labs (
    id character varying(36) NOT NULL,
    name character varying(120) NOT NULL,
    trust_domain character varying(255) NOT NULL,
    cloud character varying(20) NOT NULL,
    region character varying(64),
    vm_resource_id character varying(500),
    vm_name character varying(120),
    private_ip character varying(64),
    public_ip character varying(64),
    bind_port integer NOT NULL,
    status character varying(32) NOT NULL,
    source_cidrs text,
    firewall_name character varying(120),
    stages_done text,
    stage_job_ids text,
    deployment_mode character varying(16),
    spire_version character varying(32),
    k8s_vm_resource_id character varying(500),
    k8s_vm_name character varying(120),
    k8s_private_ip character varying(64),
    k8s_public_ip character varying(64),
    k8s_status character varying(32),
    k8s_error_message text,
    k8s_stages_done text,
    k8s_stage_job_ids text,
    k8s_audience character varying(120),
    k8s_workload_spiffe_id character varying(255),
    k8s_workload_uid integer,
    k8s_workload_role character varying(120),
    k8s_issuer_url character varying(255),
    federation_status character varying(32),
    federation_error text,
    federated_at timestamp without time zone,
    federation_proof text,
    attr_probe text,
    k8s_ansible_secret_ssh_key_source text,
    k8s_ansible_managed_account text,
    k8s_ansible_managed_become_self boolean,
    k8s_login_user character varying(104),
    entries_seeded integer,
    discovery_expected integer,
    admin_secret_folder character varying(255),
    admin_spiffe_id character varying(255),
    ps_safe character varying(120),
    trust_bundle_pem text,
    admin_svid_expires_at timestamp without time zone,
    ps_system_id character varying(36),
    ps_account_id character varying(36),
    ps_tf_state text,
    ansible_secret_ssh_key_source text,
    ansible_managed_account text,
    ansible_managed_become_self boolean,
    login_user character varying(104),
    error_message text,
    deploy_job_id character varying(36),
    workgroup character varying(100),
    created_by character varying(100),
    created_at timestamp without time zone NOT NULL,
    updated_at timestamp without time zone,
    expires_at timestamp without time zone,
    expiry_warned_at timestamp without time zone
);


--
-- Name: storage_asset_scopes; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.storage_asset_scopes (
    backend character varying(32) NOT NULL,
    name character varying(255) NOT NULL,
    workgroup character varying(64) NOT NULL,
    updated_at timestamp without time zone NOT NULL,
    updated_by character varying(100)
);


--
-- Name: users; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.users (
    id character varying(36) NOT NULL,
    username character varying(100) NOT NULL,
    hashed_password character varying(255),
    full_name character varying(200),
    email character varying(200),
    workgroups text,
    is_active boolean,
    created_at timestamp without time zone NOT NULL,
    auth_provider character varying(20) NOT NULL,
    oauth_subject character varying(255),
    mfa_required boolean,
    is_admin boolean,
    permissions text,
    session_permissions text,
    jit_permissions text,
    role_id character varying(36),
    role_permissions text,
    persona character varying(32),
    session_persona character varying(32),
    accessor_env_id character varying(36),
    pov_env_ids text,
    is_service_account boolean,
    last_login_at timestamp without time zone
);


--
-- Name: virtual_desktops; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.virtual_desktops (
    id character varying(36) NOT NULL,
    cloud character varying(20) NOT NULL,
    pool_name character varying(200) NOT NULL,
    kind character varying(20) NOT NULL,
    vm_resource_id character varying(500),
    status character varying(20) NOT NULL,
    assigned_user character varying(200),
    pra_jump_id character varying(200),
    pra_tunnel_state text,
    created_by character varying(100),
    created_at timestamp without time zone NOT NULL
);


--
-- Name: vm_workgroup_overrides; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.vm_workgroup_overrides (
    id integer NOT NULL,
    provider character varying(20) NOT NULL,
    vm_id character varying(128) NOT NULL,
    workgroup character varying(64) NOT NULL,
    created_by character varying(100),
    created_at timestamp without time zone NOT NULL,
    updated_at timestamp without time zone NOT NULL
);


--
-- Name: vm_workgroup_overrides_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.vm_workgroup_overrides_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: vm_workgroup_overrides_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.vm_workgroup_overrides_id_seq OWNED BY public.vm_workgroup_overrides.id;


--
-- Name: workgroups; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.workgroups (
    id character varying(36) NOT NULL,
    name character varying(64) NOT NULL,
    display_name character varying(200) NOT NULL,
    description text,
    local_vm_path text,
    is_default boolean NOT NULL,
    created_at timestamp without time zone NOT NULL,
    created_by_user_id character varying(36),
    change_window_id character varying(36),
    require_change_window boolean,
    deploy_defaults text,
    secret_bindings text,
    post_deploy_runs text,
    ps_attributes text,
    network_defaults text
);


--
-- Name: workload_cloud_credentials; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.workload_cloud_credentials (
    id character varying(36) NOT NULL,
    name character varying(120) NOT NULL,
    cloud character varying(20) NOT NULL,
    dynamic_name character varying(255) NOT NULL,
    dynamic_folder character varying(255),
    purpose character varying(120),
    ttl_seconds integer,
    lease_id character varying(128),
    lease_issued_at timestamp without time zone,
    lease_expires_at timestamp without time zone,
    revocable boolean,
    issue_count integer NOT NULL,
    status character varying(32) NOT NULL,
    error_message text,
    job_ids text,
    workgroup character varying(100),
    created_by character varying(100),
    created_at timestamp without time zone NOT NULL,
    updated_at timestamp without time zone,
    expires_at timestamp without time zone,
    expiry_warned_at timestamp without time zone
);


--
-- Name: workload_credential_lease; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.workload_credential_lease (
    cloud character varying(16) NOT NULL,
    purpose character varying(32) NOT NULL,
    payload text,
    lease_id character varying(128),
    issued_at timestamp without time zone,
    expires_at timestamp without time zone,
    last_attempt_at timestamp without time zone,
    last_error text,
    consecutive_failures integer NOT NULL,
    cooldown_until timestamp without time zone,
    claim_until timestamp without time zone,
    claim_owner character varying(64),
    updated_at timestamp without time zone NOT NULL
);


--
-- Name: workload_k8s_tokens; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.workload_k8s_tokens (
    id character varying(36) NOT NULL,
    name character varying(120) NOT NULL,
    cluster_id character varying(36) NOT NULL,
    cluster_name character varying(200),
    cloud character varying(20),
    profile character varying(20) NOT NULL,
    namespace character varying(253) NOT NULL,
    service_account character varying(253) NOT NULL,
    mode character varying(20) NOT NULL,
    ttl_seconds integer,
    ps_system_id character varying(64),
    ps_account_id character varying(64),
    ps_address character varying(255),
    ps_account_name character varying(255),
    ps_tf_state text,
    rotated boolean,
    status character varying(32) NOT NULL,
    error_message text,
    stages_done text,
    stage_job_ids text,
    workgroup character varying(100),
    created_by character varying(100),
    created_at timestamp without time zone NOT NULL,
    updated_at timestamp without time zone,
    expires_at timestamp without time zone,
    expiry_warned_at timestamp without time zone
);


--
-- Name: agent_enroll_attempts id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.agent_enroll_attempts ALTER COLUMN id SET DEFAULT nextval('public.agent_enroll_attempts_id_seq'::regclass);


--
-- Name: agent_nonces id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.agent_nonces ALTER COLUMN id SET DEFAULT nextval('public.agent_nonces_id_seq'::regclass);


--
-- Name: config_apply_state id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.config_apply_state ALTER COLUMN id SET DEFAULT nextval('public.config_apply_state_id_seq'::regclass);


--
-- Name: job_logs id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.job_logs ALTER COLUMN id SET DEFAULT nextval('public.job_logs_id_seq'::regclass);


--
-- Name: login_attempts id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.login_attempts ALTER COLUMN id SET DEFAULT nextval('public.login_attempts_id_seq'::regclass);


--
-- Name: vm_workgroup_overrides id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.vm_workgroup_overrides ALTER COLUMN id SET DEFAULT nextval('public.vm_workgroup_overrides_id_seq'::regclass);


--
-- Name: access_roles access_roles_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.access_roles
    ADD CONSTRAINT access_roles_pkey PRIMARY KEY (id);


--
-- Name: agent_cells agent_cells_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.agent_cells
    ADD CONSTRAINT agent_cells_pkey PRIMARY KEY (id);


--
-- Name: agent_enroll_attempts agent_enroll_attempts_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.agent_enroll_attempts
    ADD CONSTRAINT agent_enroll_attempts_pkey PRIMARY KEY (id);


--
-- Name: agent_hosts agent_hosts_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.agent_hosts
    ADD CONSTRAINT agent_hosts_pkey PRIMARY KEY (id);


--
-- Name: agent_nonces agent_nonces_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.agent_nonces
    ADD CONSTRAINT agent_nonces_pkey PRIMARY KEY (id);


--
-- Name: agent_releases agent_releases_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.agent_releases
    ADD CONSTRAINT agent_releases_pkey PRIMARY KEY (id);


--
-- Name: agent_rollouts agent_rollouts_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.agent_rollouts
    ADD CONSTRAINT agent_rollouts_pkey PRIMARY KEY (id);


--
-- Name: app_config app_config_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.app_config
    ADD CONSTRAINT app_config_pkey PRIMARY KEY (key);


--
-- Name: approvals approvals_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.approvals
    ADD CONSTRAINT approvals_pkey PRIMARY KEY (id);


--
-- Name: audit_log audit_log_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.audit_log
    ADD CONSTRAINT audit_log_pkey PRIMARY KEY (id);


--
-- Name: beyondtrust_tenants beyondtrust_tenants_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.beyondtrust_tenants
    ADD CONSTRAINT beyondtrust_tenants_pkey PRIMARY KEY (id);


--
-- Name: brand_asset brand_asset_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.brand_asset
    ADD CONSTRAINT brand_asset_pkey PRIMARY KEY (slot);


--
-- Name: cert_labs cert_labs_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.cert_labs
    ADD CONSTRAINT cert_labs_pkey PRIMARY KEY (id);


--
-- Name: change_windows change_windows_name_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.change_windows
    ADD CONSTRAINT change_windows_name_key UNIQUE (name);


--
-- Name: change_windows change_windows_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.change_windows
    ADD CONSTRAINT change_windows_pkey PRIMARY KEY (id);


--
-- Name: cloud_cost_cache cloud_cost_cache_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.cloud_cost_cache
    ADD CONSTRAINT cloud_cost_cache_pkey PRIMARY KEY (cloud, view);


--
-- Name: cloud_databases cloud_databases_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.cloud_databases
    ADD CONSTRAINT cloud_databases_pkey PRIMARY KEY (id);


--
-- Name: cloud_functions cloud_functions_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.cloud_functions
    ADD CONSTRAINT cloud_functions_pkey PRIMARY KEY (id);


--
-- Name: config_apply_state config_apply_state_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.config_apply_state
    ADD CONSTRAINT config_apply_state_pkey PRIMARY KEY (id);


--
-- Name: config_mgmt_routes config_mgmt_routes_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.config_mgmt_routes
    ADD CONSTRAINT config_mgmt_routes_pkey PRIMARY KEY (id);


--
-- Name: container_state_cache container_state_cache_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.container_state_cache
    ADD CONSTRAINT container_state_cache_pkey PRIMARY KEY (container_id);


--
-- Name: dashboard_stat_cache dashboard_stat_cache_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.dashboard_stat_cache
    ADD CONSTRAINT dashboard_stat_cache_pkey PRIMARY KEY (tile_key, scope);


--
-- Name: directory_health directory_health_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.directory_health
    ADD CONSTRAINT directory_health_pkey PRIMARY KEY (directory_id);


--
-- Name: entitle_activations entitle_activations_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.entitle_activations
    ADD CONSTRAINT entitle_activations_pkey PRIMARY KEY (id);


--
-- Name: entitle_bundle_sets entitle_bundle_sets_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.entitle_bundle_sets
    ADD CONSTRAINT entitle_bundle_sets_pkey PRIMARY KEY (id);


--
-- Name: entitle_virtual_apps entitle_virtual_apps_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.entitle_virtual_apps
    ADD CONSTRAINT entitle_virtual_apps_pkey PRIMARY KEY (id);


--
-- Name: ephemeral_state ephemeral_state_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.ephemeral_state
    ADD CONSTRAINT ephemeral_state_pkey PRIMARY KEY (key);


--
-- Name: external_workload_identities external_workload_identities_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.external_workload_identities
    ADD CONSTRAINT external_workload_identities_pkey PRIMARY KEY (id);


--
-- Name: fido2_credentials fido2_credentials_credential_id_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.fido2_credentials
    ADD CONSTRAINT fido2_credentials_credential_id_key UNIQUE (credential_id);


--
-- Name: fido2_credentials fido2_credentials_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.fido2_credentials
    ADD CONSTRAINT fido2_credentials_pkey PRIMARY KEY (id);


--
-- Name: gateways gateways_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.gateways
    ADD CONSTRAINT gateways_pkey PRIMARY KEY (id);


--
-- Name: health_areas health_areas_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.health_areas
    ADD CONSTRAINT health_areas_pkey PRIMARY KEY (area);


--
-- Name: health_checks health_checks_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.health_checks
    ADD CONSTRAINT health_checks_pkey PRIMARY KEY (area, object_id);


--
-- Name: hypervisor_connections hypervisor_connections_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.hypervisor_connections
    ADD CONSTRAINT hypervisor_connections_pkey PRIMARY KEY (id);


--
-- Name: hypervisor_vm_cache hypervisor_vm_cache_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.hypervisor_vm_cache
    ADD CONSTRAINT hypervisor_vm_cache_pkey PRIMARY KEY (connection_id, vm_id);


--
-- Name: job_logs job_logs_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.job_logs
    ADD CONSTRAINT job_logs_pkey PRIMARY KEY (id);


--
-- Name: job_schedules job_schedules_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.job_schedules
    ADD CONSTRAINT job_schedules_pkey PRIMARY KEY (id);


--
-- Name: jobs jobs_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.jobs
    ADD CONSTRAINT jobs_pkey PRIMARY KEY (id);


--
-- Name: k8s_clusters k8s_clusters_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.k8s_clusters
    ADD CONSTRAINT k8s_clusters_pkey PRIMARY KEY (id);


--
-- Name: login_attempts login_attempts_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.login_attempts
    ADD CONSTRAINT login_attempts_pkey PRIMARY KEY (id);


--
-- Name: managed_directories managed_directories_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.managed_directories
    ADD CONSTRAINT managed_directories_pkey PRIMARY KEY (id);


--
-- Name: notification_deliveries notification_deliveries_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.notification_deliveries
    ADD CONSTRAINT notification_deliveries_pkey PRIMARY KEY (id);


--
-- Name: notification_endpoints notification_endpoints_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.notification_endpoints
    ADD CONSTRAINT notification_endpoints_pkey PRIMARY KEY (id);


--
-- Name: oauth_clients oauth_clients_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.oauth_clients
    ADD CONSTRAINT oauth_clients_pkey PRIMARY KEY (id);


--
-- Name: oauth_group_mappings oauth_group_mappings_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.oauth_group_mappings
    ADD CONSTRAINT oauth_group_mappings_pkey PRIMARY KEY (id);


--
-- Name: personal_access_tokens personal_access_tokens_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.personal_access_tokens
    ADD CONSTRAINT personal_access_tokens_pkey PRIMARY KEY (id);


--
-- Name: pov_accessors pov_accessors_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.pov_accessors
    ADD CONSTRAINT pov_accessors_pkey PRIMARY KEY (id);


--
-- Name: pov_blueprints pov_blueprints_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.pov_blueprints
    ADD CONSTRAINT pov_blueprints_pkey PRIMARY KEY (id);


--
-- Name: pov_cloud_template_vms pov_cloud_template_vms_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.pov_cloud_template_vms
    ADD CONSTRAINT pov_cloud_template_vms_pkey PRIMARY KEY (id);


--
-- Name: pov_cloud_templates pov_cloud_templates_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.pov_cloud_templates
    ADD CONSTRAINT pov_cloud_templates_pkey PRIMARY KEY (id);


--
-- Name: pov_environment_vms pov_environment_vms_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.pov_environment_vms
    ADD CONSTRAINT pov_environment_vms_pkey PRIMARY KEY (id);


--
-- Name: pov_environments pov_environments_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.pov_environments
    ADD CONSTRAINT pov_environments_pkey PRIMARY KEY (id);


--
-- Name: pov_platform_cache pov_platform_cache_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.pov_platform_cache
    ADD CONSTRAINT pov_platform_cache_pkey PRIMARY KEY (kind, platform, project_id);


--
-- Name: pov_signoffs pov_signoffs_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.pov_signoffs
    ADD CONSTRAINT pov_signoffs_pkey PRIMARY KEY (id);


--
-- Name: pov_template_builds pov_template_builds_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.pov_template_builds
    ADD CONSTRAINT pov_template_builds_pkey PRIMARY KEY (id);


--
-- Name: pov_use_case_progress pov_use_case_progress_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.pov_use_case_progress
    ADD CONSTRAINT pov_use_case_progress_pkey PRIMARY KEY (id);


--
-- Name: pov_vendor_users pov_vendor_users_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.pov_vendor_users
    ADD CONSTRAINT pov_vendor_users_pkey PRIMARY KEY (id);


--
-- Name: registered_images registered_images_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.registered_images
    ADD CONSTRAINT registered_images_pkey PRIMARY KEY (id);


--
-- Name: remote_agents remote_agents_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.remote_agents
    ADD CONSTRAINT remote_agents_pkey PRIMARY KEY (id);


--
-- Name: runner_credential_grants runner_credential_grants_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.runner_credential_grants
    ADD CONSTRAINT runner_credential_grants_pkey PRIMARY KEY (id);


--
-- Name: schema_markers schema_markers_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.schema_markers
    ADD CONSTRAINT schema_markers_pkey PRIMARY KEY (key);


--
-- Name: secret_vaults secret_vaults_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.secret_vaults
    ADD CONSTRAINT secret_vaults_pkey PRIMARY KEY (id);


--
-- Name: spiffe_trust_domains spiffe_trust_domains_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.spiffe_trust_domains
    ADD CONSTRAINT spiffe_trust_domains_pkey PRIMARY KEY (id);


--
-- Name: spire_admin_calls spire_admin_calls_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.spire_admin_calls
    ADD CONSTRAINT spire_admin_calls_pkey PRIMARY KEY (id);


--
-- Name: spire_admin_relays spire_admin_relays_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.spire_admin_relays
    ADD CONSTRAINT spire_admin_relays_pkey PRIMARY KEY (id);


--
-- Name: spire_labs spire_labs_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.spire_labs
    ADD CONSTRAINT spire_labs_pkey PRIMARY KEY (id);


--
-- Name: storage_asset_scopes storage_asset_scopes_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.storage_asset_scopes
    ADD CONSTRAINT storage_asset_scopes_pkey PRIMARY KEY (backend, name);


--
-- Name: agent_nonces uq_agent_nonce; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.agent_nonces
    ADD CONSTRAINT uq_agent_nonce UNIQUE (agent_id, nonce);


--
-- Name: config_apply_state uq_apply_target_playbook; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.config_apply_state
    ADD CONSTRAINT uq_apply_target_playbook UNIQUE (target, playbook_ref);


--
-- Name: config_mgmt_routes uq_config_mgmt_route_scope_net; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.config_mgmt_routes
    ADD CONSTRAINT uq_config_mgmt_route_scope_net UNIQUE (cidr, scope, scope_region, scope_connection_id, scope_workstream, scope_network);


--
-- Name: external_workload_identities uq_external_workload_iss_sub; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.external_workload_identities
    ADD CONSTRAINT uq_external_workload_iss_sub UNIQUE (issuer, subject);


--
-- Name: hypervisor_connections uq_hypervisor_connection_name; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.hypervisor_connections
    ADD CONSTRAINT uq_hypervisor_connection_name UNIQUE (kind, name);


--
-- Name: job_logs uq_job_log_seq; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.job_logs
    ADD CONSTRAINT uq_job_log_seq UNIQUE (job_id, seq);


--
-- Name: notification_deliveries uq_notification_dedupe; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.notification_deliveries
    ADD CONSTRAINT uq_notification_dedupe UNIQUE (dedupe_key);


--
-- Name: pov_use_case_progress uq_pov_use_case_progress_env_card; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.pov_use_case_progress
    ADD CONSTRAINT uq_pov_use_case_progress_env_card UNIQUE (environment_id, card_id);


--
-- Name: vm_workgroup_overrides uq_vm_workgroup_override; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.vm_workgroup_overrides
    ADD CONSTRAINT uq_vm_workgroup_override UNIQUE (provider, vm_id);


--
-- Name: users users_oauth_subject_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.users
    ADD CONSTRAINT users_oauth_subject_key UNIQUE (oauth_subject);


--
-- Name: users users_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.users
    ADD CONSTRAINT users_pkey PRIMARY KEY (id);


--
-- Name: virtual_desktops virtual_desktops_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.virtual_desktops
    ADD CONSTRAINT virtual_desktops_pkey PRIMARY KEY (id);


--
-- Name: vm_workgroup_overrides vm_workgroup_overrides_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.vm_workgroup_overrides
    ADD CONSTRAINT vm_workgroup_overrides_pkey PRIMARY KEY (id);


--
-- Name: workgroups workgroups_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.workgroups
    ADD CONSTRAINT workgroups_pkey PRIMARY KEY (id);


--
-- Name: workload_cloud_credentials workload_cloud_credentials_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.workload_cloud_credentials
    ADD CONSTRAINT workload_cloud_credentials_pkey PRIMARY KEY (id);


--
-- Name: workload_credential_lease workload_credential_lease_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.workload_credential_lease
    ADD CONSTRAINT workload_credential_lease_pkey PRIMARY KEY (cloud, purpose);


--
-- Name: workload_k8s_tokens workload_k8s_tokens_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.workload_k8s_tokens
    ADD CONSTRAINT workload_k8s_tokens_pkey PRIMARY KEY (id);


--
-- Name: ix_access_roles_slug; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX ix_access_roles_slug ON public.access_roles USING btree (slug);


--
-- Name: ix_agent_cells_status; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_agent_cells_status ON public.agent_cells USING btree (status);


--
-- Name: ix_agent_enroll_attempts_attempted_at; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_agent_enroll_attempts_attempted_at ON public.agent_enroll_attempts USING btree (attempted_at);


--
-- Name: ix_agent_enroll_attempts_ip; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_agent_enroll_attempts_ip ON public.agent_enroll_attempts USING btree (ip);


--
-- Name: ix_agent_hosts_agent_id; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX ix_agent_hosts_agent_id ON public.agent_hosts USING btree (agent_id);


--
-- Name: ix_agent_hosts_cloud; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_agent_hosts_cloud ON public.agent_hosts USING btree (cloud);


--
-- Name: ix_agent_hosts_status; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_agent_hosts_status ON public.agent_hosts USING btree (status);


--
-- Name: ix_agent_nonces_agent_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_agent_nonces_agent_id ON public.agent_nonces USING btree (agent_id);


--
-- Name: ix_agent_nonces_seen_at; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_agent_nonces_seen_at ON public.agent_nonces USING btree (seen_at);


--
-- Name: ix_agent_releases_version; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX ix_agent_releases_version ON public.agent_releases USING btree (version);


--
-- Name: ix_app_config_key_workgroup; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_app_config_key_workgroup ON public.app_config USING btree (key, workgroup);


--
-- Name: ix_approvals_action; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_approvals_action ON public.approvals USING btree (action);


--
-- Name: ix_approvals_entitle_request_id; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX ix_approvals_entitle_request_id ON public.approvals USING btree (entitle_request_id);


--
-- Name: ix_approvals_principal_kind; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_approvals_principal_kind ON public.approvals USING btree (principal_kind);


--
-- Name: ix_approvals_status; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_approvals_status ON public.approvals USING btree (status);


--
-- Name: ix_approvals_user_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_approvals_user_id ON public.approvals USING btree (user_id);


--
-- Name: ix_audit_log_action; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_audit_log_action ON public.audit_log USING btree (action);


--
-- Name: ix_audit_log_seq; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX ix_audit_log_seq ON public.audit_log USING btree (seq);


--
-- Name: ix_audit_log_timestamp; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_audit_log_timestamp ON public.audit_log USING btree ("timestamp");


--
-- Name: ix_audit_log_username; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_audit_log_username ON public.audit_log USING btree (username);


--
-- Name: ix_beyondtrust_tenants_kind; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_beyondtrust_tenants_kind ON public.beyondtrust_tenants USING btree (kind);


--
-- Name: ix_cert_labs_expires_at; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_cert_labs_expires_at ON public.cert_labs USING btree (expires_at);


--
-- Name: ix_cert_labs_status; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_cert_labs_status ON public.cert_labs USING btree (status);


--
-- Name: ix_cert_labs_workgroup; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_cert_labs_workgroup ON public.cert_labs USING btree (workgroup);


--
-- Name: ix_cloud_cost_cache_cooldown_until; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_cloud_cost_cache_cooldown_until ON public.cloud_cost_cache USING btree (cooldown_until);


--
-- Name: ix_cloud_cost_cache_fetched_at; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_cloud_cost_cache_fetched_at ON public.cloud_cost_cache USING btree (fetched_at);


--
-- Name: ix_cloud_databases_agent_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_cloud_databases_agent_id ON public.cloud_databases USING btree (agent_id);


--
-- Name: ix_cloud_databases_expires_at; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_cloud_databases_expires_at ON public.cloud_databases USING btree (expires_at);


--
-- Name: ix_cloud_databases_status; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_cloud_databases_status ON public.cloud_databases USING btree (status);


--
-- Name: ix_cloud_databases_workgroup; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_cloud_databases_workgroup ON public.cloud_databases USING btree (workgroup);


--
-- Name: ix_cloud_functions_name; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_cloud_functions_name ON public.cloud_functions USING btree (name);


--
-- Name: ix_cloud_functions_status; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_cloud_functions_status ON public.cloud_functions USING btree (status);


--
-- Name: ix_config_mgmt_routes_agent_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_config_mgmt_routes_agent_id ON public.config_mgmt_routes USING btree (agent_id);


--
-- Name: ix_container_state_cache_endpoint_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_container_state_cache_endpoint_id ON public.container_state_cache USING btree (endpoint_id);


--
-- Name: ix_container_state_cache_last_updated; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_container_state_cache_last_updated ON public.container_state_cache USING btree (last_updated);


--
-- Name: ix_container_state_cache_workgroup; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_container_state_cache_workgroup ON public.container_state_cache USING btree (workgroup);


--
-- Name: ix_dashboard_stat_cache_cooldown_until; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_dashboard_stat_cache_cooldown_until ON public.dashboard_stat_cache USING btree (cooldown_until);


--
-- Name: ix_dashboard_stat_cache_fetched_at; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_dashboard_stat_cache_fetched_at ON public.dashboard_stat_cache USING btree (fetched_at);


--
-- Name: ix_dashboard_stat_cache_provider; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_dashboard_stat_cache_provider ON public.dashboard_stat_cache USING btree (provider);


--
-- Name: ix_directory_health_checked_at; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_directory_health_checked_at ON public.directory_health USING btree (checked_at);


--
-- Name: ix_directory_health_deep_job_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_directory_health_deep_job_id ON public.directory_health USING btree (deep_job_id);


--
-- Name: ix_directory_health_pending_job_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_directory_health_pending_job_id ON public.directory_health USING btree (pending_job_id);


--
-- Name: ix_entitle_activations_cloud; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_entitle_activations_cloud ON public.entitle_activations USING btree (cloud);


--
-- Name: ix_entitle_activations_entitle_request_id; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX ix_entitle_activations_entitle_request_id ON public.entitle_activations USING btree (entitle_request_id);


--
-- Name: ix_entitle_activations_operation; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_entitle_activations_operation ON public.entitle_activations USING btree (operation);


--
-- Name: ix_entitle_activations_requester_user_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_entitle_activations_requester_user_id ON public.entitle_activations USING btree (requester_user_id);


--
-- Name: ix_entitle_activations_status; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_entitle_activations_status ON public.entitle_activations USING btree (status);


--
-- Name: ix_entitle_activations_tenant_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_entitle_activations_tenant_id ON public.entitle_activations USING btree (tenant_id);


--
-- Name: ix_entitle_bundle_sets_status; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_entitle_bundle_sets_status ON public.entitle_bundle_sets USING btree (status);


--
-- Name: ix_entitle_virtual_apps_directory_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_entitle_virtual_apps_directory_id ON public.entitle_virtual_apps USING btree (directory_id);


--
-- Name: ix_entitle_virtual_apps_status; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_entitle_virtual_apps_status ON public.entitle_virtual_apps USING btree (status);


--
-- Name: ix_ephemeral_state_expires_at; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_ephemeral_state_expires_at ON public.ephemeral_state USING btree (expires_at);


--
-- Name: ix_external_workload_identities_user_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_external_workload_identities_user_id ON public.external_workload_identities USING btree (user_id);


--
-- Name: ix_fido2_credentials_user_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_fido2_credentials_user_id ON public.fido2_credentials USING btree (user_id);


--
-- Name: ix_gateways_cloud; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_gateways_cloud ON public.gateways USING btree (cloud);


--
-- Name: ix_gateways_managed; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_gateways_managed ON public.gateways USING btree (managed);


--
-- Name: ix_gateways_name; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_gateways_name ON public.gateways USING btree (name);


--
-- Name: ix_gateways_region; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_gateways_region ON public.gateways USING btree (region);


--
-- Name: ix_gateways_status; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_gateways_status ON public.gateways USING btree (status);


--
-- Name: ix_health_checks_status; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_health_checks_status ON public.health_checks USING btree (status);


--
-- Name: ix_hypervisor_connections_agent_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_hypervisor_connections_agent_id ON public.hypervisor_connections USING btree (agent_id);


--
-- Name: ix_hypervisor_connections_kind; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_hypervisor_connections_kind ON public.hypervisor_connections USING btree (kind);


--
-- Name: ix_hypervisor_connections_site; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_hypervisor_connections_site ON public.hypervisor_connections USING btree (site);


--
-- Name: ix_hypervisor_vm_cache_synced_at; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_hypervisor_vm_cache_synced_at ON public.hypervisor_vm_cache USING btree (synced_at);


--
-- Name: ix_job_logs_job_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_job_logs_job_id ON public.job_logs USING btree (job_id);


--
-- Name: ix_job_schedules_change_window_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_job_schedules_change_window_id ON public.job_schedules USING btree (change_window_id);


--
-- Name: ix_job_schedules_created_by; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_job_schedules_created_by ON public.job_schedules USING btree (created_by);


--
-- Name: ix_jobs_agent_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_jobs_agent_id ON public.jobs USING btree (agent_id);


--
-- Name: ix_jobs_batch_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_jobs_batch_id ON public.jobs USING btree (batch_id);


--
-- Name: ix_jobs_cloud_resource_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_jobs_cloud_resource_id ON public.jobs USING btree (cloud_resource_id);


--
-- Name: ix_jobs_created_at; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_jobs_created_at ON public.jobs USING btree (created_at);


--
-- Name: ix_jobs_created_by; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_jobs_created_by ON public.jobs USING btree (created_by);


--
-- Name: ix_jobs_expires_at; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_jobs_expires_at ON public.jobs USING btree (expires_at);


--
-- Name: ix_jobs_is_checkin; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_jobs_is_checkin ON public.jobs USING btree (is_checkin);


--
-- Name: ix_jobs_job_schedule_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_jobs_job_schedule_id ON public.jobs USING btree (job_schedule_id);


--
-- Name: ix_jobs_job_type; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_jobs_job_type ON public.jobs USING btree (job_type);


--
-- Name: ix_jobs_scheduled_for; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_jobs_scheduled_for ON public.jobs USING btree (scheduled_for);


--
-- Name: ix_jobs_status; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_jobs_status ON public.jobs USING btree (status);


--
-- Name: ix_jobs_workgroup; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_jobs_workgroup ON public.jobs USING btree (workgroup);


--
-- Name: ix_k8s_clusters_expires_at; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_k8s_clusters_expires_at ON public.k8s_clusters USING btree (expires_at);


--
-- Name: ix_k8s_clusters_name; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_k8s_clusters_name ON public.k8s_clusters USING btree (name);


--
-- Name: ix_k8s_clusters_ps_token_account_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_k8s_clusters_ps_token_account_id ON public.k8s_clusters USING btree (ps_token_account_id);


--
-- Name: ix_k8s_clusters_status; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_k8s_clusters_status ON public.k8s_clusters USING btree (status);


--
-- Name: ix_k8s_clusters_workgroup; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_k8s_clusters_workgroup ON public.k8s_clusters USING btree (workgroup);


--
-- Name: ix_login_attempts_attempted_at; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_login_attempts_attempted_at ON public.login_attempts USING btree (attempted_at);


--
-- Name: ix_login_attempts_ip; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_login_attempts_ip ON public.login_attempts USING btree (ip);


--
-- Name: ix_login_attempts_username; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_login_attempts_username ON public.login_attempts USING btree (username);


--
-- Name: ix_managed_directories_agent_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_managed_directories_agent_id ON public.managed_directories USING btree (agent_id);


--
-- Name: ix_managed_directories_expires_at; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_managed_directories_expires_at ON public.managed_directories USING btree (expires_at);


--
-- Name: ix_managed_directories_linked_directory_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_managed_directories_linked_directory_id ON public.managed_directories USING btree (linked_directory_id);


--
-- Name: ix_managed_directories_status; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_managed_directories_status ON public.managed_directories USING btree (status);


--
-- Name: ix_managed_directories_workgroup; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_managed_directories_workgroup ON public.managed_directories USING btree (workgroup);


--
-- Name: ix_notification_deliveries_created_at; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_notification_deliveries_created_at ON public.notification_deliveries USING btree (created_at);


--
-- Name: ix_notification_deliveries_endpoint_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_notification_deliveries_endpoint_id ON public.notification_deliveries USING btree (endpoint_id);


--
-- Name: ix_notification_deliveries_event_type; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_notification_deliveries_event_type ON public.notification_deliveries USING btree (event_type);


--
-- Name: ix_notification_deliveries_next_attempt_at; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_notification_deliveries_next_attempt_at ON public.notification_deliveries USING btree (next_attempt_at);


--
-- Name: ix_notification_deliveries_resource_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_notification_deliveries_resource_id ON public.notification_deliveries USING btree (resource_id);


--
-- Name: ix_notification_deliveries_status; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_notification_deliveries_status ON public.notification_deliveries USING btree (status);


--
-- Name: ix_notification_deliveries_workgroup; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_notification_deliveries_workgroup ON public.notification_deliveries USING btree (workgroup);


--
-- Name: ix_oauth_clients_client_id; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX ix_oauth_clients_client_id ON public.oauth_clients USING btree (client_id);


--
-- Name: ix_oauth_clients_secret_hash; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_oauth_clients_secret_hash ON public.oauth_clients USING btree (secret_hash);


--
-- Name: ix_oauth_clients_spiffe_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_oauth_clients_spiffe_id ON public.oauth_clients USING btree (spiffe_id);


--
-- Name: ix_oauth_clients_user_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_oauth_clients_user_id ON public.oauth_clients USING btree (user_id);


--
-- Name: ix_oauth_group_mappings_entra_group_id; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX ix_oauth_group_mappings_entra_group_id ON public.oauth_group_mappings USING btree (entra_group_id);


--
-- Name: ix_personal_access_tokens_token_hash; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX ix_personal_access_tokens_token_hash ON public.personal_access_tokens USING btree (token_hash);


--
-- Name: ix_personal_access_tokens_user_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_personal_access_tokens_user_id ON public.personal_access_tokens USING btree (user_id);


--
-- Name: ix_pov_accessors_entitle_actor_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_pov_accessors_entitle_actor_id ON public.pov_accessors USING btree (entitle_actor_id);


--
-- Name: ix_pov_accessors_environment_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_pov_accessors_environment_id ON public.pov_accessors USING btree (environment_id);


--
-- Name: ix_pov_accessors_expires_at; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_pov_accessors_expires_at ON public.pov_accessors USING btree (expires_at);


--
-- Name: ix_pov_accessors_user_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_pov_accessors_user_id ON public.pov_accessors USING btree (user_id);


--
-- Name: ix_pov_blueprints_entitle_tenant_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_pov_blueprints_entitle_tenant_id ON public.pov_blueprints USING btree (entitle_tenant_id);


--
-- Name: ix_pov_blueprints_name; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_pov_blueprints_name ON public.pov_blueprints USING btree (name);


--
-- Name: ix_pov_blueprints_pra_tenant_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_pov_blueprints_pra_tenant_id ON public.pov_blueprints USING btree (pra_tenant_id);


--
-- Name: ix_pov_blueprints_ps_tenant_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_pov_blueprints_ps_tenant_id ON public.pov_blueprints USING btree (ps_tenant_id);


--
-- Name: ix_pov_blueprints_workgroup; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_pov_blueprints_workgroup ON public.pov_blueprints USING btree (workgroup);


--
-- Name: ix_pov_cloud_template_vms_template_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_pov_cloud_template_vms_template_id ON public.pov_cloud_template_vms USING btree (template_id);


--
-- Name: ix_pov_cloud_templates_cloud; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_pov_cloud_templates_cloud ON public.pov_cloud_templates USING btree (cloud);


--
-- Name: ix_pov_cloud_templates_name; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_pov_cloud_templates_name ON public.pov_cloud_templates USING btree (name);


--
-- Name: ix_pov_cloud_templates_workgroup; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_pov_cloud_templates_workgroup ON public.pov_cloud_templates USING btree (workgroup);


--
-- Name: ix_pov_environment_vms_environment_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_pov_environment_vms_environment_id ON public.pov_environment_vms USING btree (environment_id);


--
-- Name: ix_pov_environments_entitle_tenant_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_pov_environments_entitle_tenant_id ON public.pov_environments USING btree (entitle_tenant_id);


--
-- Name: ix_pov_environments_expires_at; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_pov_environments_expires_at ON public.pov_environments USING btree (expires_at);


--
-- Name: ix_pov_environments_platform_environment_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_pov_environments_platform_environment_id ON public.pov_environments USING btree (platform_environment_id);


--
-- Name: ix_pov_environments_pra_tenant_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_pov_environments_pra_tenant_id ON public.pov_environments USING btree (pra_tenant_id);


--
-- Name: ix_pov_environments_provision_job_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_pov_environments_provision_job_id ON public.pov_environments USING btree (provision_job_id);


--
-- Name: ix_pov_environments_ps_tenant_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_pov_environments_ps_tenant_id ON public.pov_environments USING btree (ps_tenant_id);


--
-- Name: ix_pov_environments_status; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_pov_environments_status ON public.pov_environments USING btree (status);


--
-- Name: ix_pov_environments_workgroup; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_pov_environments_workgroup ON public.pov_environments USING btree (workgroup);


--
-- Name: ix_pov_platform_cache_cooldown_until; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_pov_platform_cache_cooldown_until ON public.pov_platform_cache USING btree (cooldown_until);


--
-- Name: ix_pov_platform_cache_fetched_at; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_pov_platform_cache_fetched_at ON public.pov_platform_cache USING btree (fetched_at);


--
-- Name: ix_pov_signoffs_environment_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_pov_signoffs_environment_id ON public.pov_signoffs USING btree (environment_id);


--
-- Name: ix_pov_template_builds_build_environment_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_pov_template_builds_build_environment_id ON public.pov_template_builds USING btree (build_environment_id);


--
-- Name: ix_pov_template_builds_job_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_pov_template_builds_job_id ON public.pov_template_builds USING btree (job_id);


--
-- Name: ix_pov_template_builds_result_template_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_pov_template_builds_result_template_id ON public.pov_template_builds USING btree (result_template_id);


--
-- Name: ix_pov_template_builds_status; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_pov_template_builds_status ON public.pov_template_builds USING btree (status);


--
-- Name: ix_pov_template_builds_workgroup; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_pov_template_builds_workgroup ON public.pov_template_builds USING btree (workgroup);


--
-- Name: ix_pov_use_case_progress_environment_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_pov_use_case_progress_environment_id ON public.pov_use_case_progress USING btree (environment_id);


--
-- Name: ix_pov_vendor_users_environment_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_pov_vendor_users_environment_id ON public.pov_vendor_users USING btree (environment_id);


--
-- Name: ix_pov_vendor_users_expires_at; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_pov_vendor_users_expires_at ON public.pov_vendor_users USING btree (expires_at);


--
-- Name: ix_registered_images_name; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_registered_images_name ON public.registered_images USING btree (name);


--
-- Name: ix_remote_agents_enroll_code_hash; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX ix_remote_agents_enroll_code_hash ON public.remote_agents USING btree (enroll_code_hash);


--
-- Name: ix_remote_agents_last_seen_at; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_remote_agents_last_seen_at ON public.remote_agents USING btree (last_seen_at);


--
-- Name: ix_remote_agents_name; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX ix_remote_agents_name ON public.remote_agents USING btree (name);


--
-- Name: ix_remote_agents_site; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_remote_agents_site ON public.remote_agents USING btree (site);


--
-- Name: ix_remote_agents_spiffe_id; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX ix_remote_agents_spiffe_id ON public.remote_agents USING btree (spiffe_id);


--
-- Name: ix_runner_credential_grants_job_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_runner_credential_grants_job_id ON public.runner_credential_grants USING btree (job_id);


--
-- Name: ix_runner_credential_grants_token_hash; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX ix_runner_credential_grants_token_hash ON public.runner_credential_grants USING btree (token_hash);


--
-- Name: ix_spiffe_trust_domains_trust_domain; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX ix_spiffe_trust_domains_trust_domain ON public.spiffe_trust_domains USING btree (trust_domain);


--
-- Name: ix_spire_admin_calls_created_at; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_spire_admin_calls_created_at ON public.spire_admin_calls USING btree (created_at);


--
-- Name: ix_spire_admin_calls_status; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_spire_admin_calls_status ON public.spire_admin_calls USING btree (status);


--
-- Name: ix_spire_admin_relays_seen_at; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_spire_admin_relays_seen_at ON public.spire_admin_relays USING btree (seen_at);


--
-- Name: ix_spire_labs_expires_at; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_spire_labs_expires_at ON public.spire_labs USING btree (expires_at);


--
-- Name: ix_spire_labs_status; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_spire_labs_status ON public.spire_labs USING btree (status);


--
-- Name: ix_spire_labs_workgroup; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_spire_labs_workgroup ON public.spire_labs USING btree (workgroup);


--
-- Name: ix_storage_asset_scopes_workgroup; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_storage_asset_scopes_workgroup ON public.storage_asset_scopes USING btree (workgroup);


--
-- Name: ix_users_accessor_env_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_users_accessor_env_id ON public.users USING btree (accessor_env_id);


--
-- Name: ix_users_role_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_users_role_id ON public.users USING btree (role_id);


--
-- Name: ix_users_username; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX ix_users_username ON public.users USING btree (username);


--
-- Name: ix_virtual_desktops_pool_name; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_virtual_desktops_pool_name ON public.virtual_desktops USING btree (pool_name);


--
-- Name: ix_virtual_desktops_status; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_virtual_desktops_status ON public.virtual_desktops USING btree (status);


--
-- Name: ix_vm_workgroup_overrides_provider; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_vm_workgroup_overrides_provider ON public.vm_workgroup_overrides USING btree (provider);


--
-- Name: ix_vm_workgroup_overrides_workgroup; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_vm_workgroup_overrides_workgroup ON public.vm_workgroup_overrides USING btree (workgroup);


--
-- Name: ix_workgroups_is_default; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_workgroups_is_default ON public.workgroups USING btree (is_default);


--
-- Name: ix_workgroups_name; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX ix_workgroups_name ON public.workgroups USING btree (name);


--
-- Name: ix_workload_cloud_credentials_cloud; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_workload_cloud_credentials_cloud ON public.workload_cloud_credentials USING btree (cloud);


--
-- Name: ix_workload_cloud_credentials_expires_at; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_workload_cloud_credentials_expires_at ON public.workload_cloud_credentials USING btree (expires_at);


--
-- Name: ix_workload_cloud_credentials_lease_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_workload_cloud_credentials_lease_id ON public.workload_cloud_credentials USING btree (lease_id);


--
-- Name: ix_workload_cloud_credentials_status; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_workload_cloud_credentials_status ON public.workload_cloud_credentials USING btree (status);


--
-- Name: ix_workload_cloud_credentials_workgroup; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_workload_cloud_credentials_workgroup ON public.workload_cloud_credentials USING btree (workgroup);


--
-- Name: ix_workload_credential_lease_expires_at; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_workload_credential_lease_expires_at ON public.workload_credential_lease USING btree (expires_at);


--
-- Name: ix_workload_k8s_tokens_cluster_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_workload_k8s_tokens_cluster_id ON public.workload_k8s_tokens USING btree (cluster_id);


--
-- Name: ix_workload_k8s_tokens_expires_at; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_workload_k8s_tokens_expires_at ON public.workload_k8s_tokens USING btree (expires_at);


--
-- Name: ix_workload_k8s_tokens_ps_account_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_workload_k8s_tokens_ps_account_id ON public.workload_k8s_tokens USING btree (ps_account_id);


--
-- Name: ix_workload_k8s_tokens_status; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_workload_k8s_tokens_status ON public.workload_k8s_tokens USING btree (status);


--
-- Name: ix_workload_k8s_tokens_workgroup; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_workload_k8s_tokens_workgroup ON public.workload_k8s_tokens USING btree (workgroup);


--
-- Name: access_roles access_roles_created_by_user_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.access_roles
    ADD CONSTRAINT access_roles_created_by_user_id_fkey FOREIGN KEY (created_by_user_id) REFERENCES public.users(id) ON DELETE SET NULL;


--
-- Name: agent_hosts agent_hosts_agent_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.agent_hosts
    ADD CONSTRAINT agent_hosts_agent_id_fkey FOREIGN KEY (agent_id) REFERENCES public.remote_agents(id) ON DELETE CASCADE;


--
-- Name: agent_rollouts agent_rollouts_release_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.agent_rollouts
    ADD CONSTRAINT agent_rollouts_release_id_fkey FOREIGN KEY (release_id) REFERENCES public.agent_releases(id);


--
-- Name: approvals approvals_user_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.approvals
    ADD CONSTRAINT approvals_user_id_fkey FOREIGN KEY (user_id) REFERENCES public.users(id) ON DELETE CASCADE;


--
-- Name: cloud_databases cloud_databases_agent_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.cloud_databases
    ADD CONSTRAINT cloud_databases_agent_id_fkey FOREIGN KEY (agent_id) REFERENCES public.remote_agents(id) ON DELETE SET NULL;


--
-- Name: config_mgmt_routes config_mgmt_routes_agent_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.config_mgmt_routes
    ADD CONSTRAINT config_mgmt_routes_agent_id_fkey FOREIGN KEY (agent_id) REFERENCES public.remote_agents(id) ON DELETE CASCADE;


--
-- Name: entitle_activations entitle_activations_requester_user_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.entitle_activations
    ADD CONSTRAINT entitle_activations_requester_user_id_fkey FOREIGN KEY (requester_user_id) REFERENCES public.users(id) ON DELETE SET NULL;


--
-- Name: external_workload_identities external_workload_identities_user_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.external_workload_identities
    ADD CONSTRAINT external_workload_identities_user_id_fkey FOREIGN KEY (user_id) REFERENCES public.users(id) ON DELETE CASCADE;


--
-- Name: fido2_credentials fido2_credentials_user_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.fido2_credentials
    ADD CONSTRAINT fido2_credentials_user_id_fkey FOREIGN KEY (user_id) REFERENCES public.users(id) ON DELETE CASCADE;


--
-- Name: hypervisor_connections hypervisor_connections_agent_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.hypervisor_connections
    ADD CONSTRAINT hypervisor_connections_agent_id_fkey FOREIGN KEY (agent_id) REFERENCES public.remote_agents(id) ON DELETE SET NULL;


--
-- Name: jobs jobs_agent_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.jobs
    ADD CONSTRAINT jobs_agent_id_fkey FOREIGN KEY (agent_id) REFERENCES public.remote_agents(id) ON DELETE SET NULL;


--
-- Name: managed_directories managed_directories_agent_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.managed_directories
    ADD CONSTRAINT managed_directories_agent_id_fkey FOREIGN KEY (agent_id) REFERENCES public.remote_agents(id) ON DELETE SET NULL;


--
-- Name: oauth_clients oauth_clients_user_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.oauth_clients
    ADD CONSTRAINT oauth_clients_user_id_fkey FOREIGN KEY (user_id) REFERENCES public.users(id) ON DELETE CASCADE;


--
-- Name: personal_access_tokens personal_access_tokens_user_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.personal_access_tokens
    ADD CONSTRAINT personal_access_tokens_user_id_fkey FOREIGN KEY (user_id) REFERENCES public.users(id) ON DELETE CASCADE;


--
-- Name: pov_blueprints pov_blueprints_entitle_tenant_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.pov_blueprints
    ADD CONSTRAINT pov_blueprints_entitle_tenant_id_fkey FOREIGN KEY (entitle_tenant_id) REFERENCES public.beyondtrust_tenants(id) ON DELETE SET NULL;


--
-- Name: pov_blueprints pov_blueprints_pra_tenant_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.pov_blueprints
    ADD CONSTRAINT pov_blueprints_pra_tenant_id_fkey FOREIGN KEY (pra_tenant_id) REFERENCES public.beyondtrust_tenants(id) ON DELETE SET NULL;


--
-- Name: pov_blueprints pov_blueprints_ps_tenant_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.pov_blueprints
    ADD CONSTRAINT pov_blueprints_ps_tenant_id_fkey FOREIGN KEY (ps_tenant_id) REFERENCES public.beyondtrust_tenants(id) ON DELETE SET NULL;


--
-- Name: pov_environments pov_environments_entitle_tenant_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.pov_environments
    ADD CONSTRAINT pov_environments_entitle_tenant_id_fkey FOREIGN KEY (entitle_tenant_id) REFERENCES public.beyondtrust_tenants(id) ON DELETE SET NULL;


--
-- Name: pov_environments pov_environments_pra_tenant_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.pov_environments
    ADD CONSTRAINT pov_environments_pra_tenant_id_fkey FOREIGN KEY (pra_tenant_id) REFERENCES public.beyondtrust_tenants(id) ON DELETE SET NULL;


--
-- Name: pov_environments pov_environments_ps_tenant_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.pov_environments
    ADD CONSTRAINT pov_environments_ps_tenant_id_fkey FOREIGN KEY (ps_tenant_id) REFERENCES public.beyondtrust_tenants(id) ON DELETE SET NULL;


--
-- Name: vm_workgroup_overrides vm_workgroup_overrides_workgroup_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.vm_workgroup_overrides
    ADD CONSTRAINT vm_workgroup_overrides_workgroup_fkey FOREIGN KEY (workgroup) REFERENCES public.workgroups(name) ON DELETE CASCADE;


--
-- Name: workgroups workgroups_created_by_user_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.workgroups
    ADD CONSTRAINT workgroups_created_by_user_id_fkey FOREIGN KEY (created_by_user_id) REFERENCES public.users(id) ON DELETE SET NULL;


--
-- PostgreSQL database dump complete
--


--
-- PostgreSQL database dump
--



SET statement_timeout = 0;
SET lock_timeout = 0;
SET idle_in_transaction_session_timeout = 0;
SET client_encoding = 'UTF8';
SET standard_conforming_strings = on;
SELECT pg_catalog.set_config('search_path', '', false);
SET check_function_bodies = false;
SET xmloption = content;
SET client_min_messages = warning;
SET row_security = off;

--
-- Data for Name: schema_markers; Type: TABLE DATA; Schema: public; Owner: -
--

INSERT INTO public.schema_markers VALUES ('ddl:legacy_statements_v1', '2026-11-04 18:09:57.175862', 'all 222 legacy statements applied');
INSERT INTO public.schema_markers VALUES ('ddl:0001_config_route_cloud_vms', '2026-11-04 18:09:57.181709', 'add config_mgmt_routes.include_cloud_vms');
INSERT INTO public.schema_markers VALUES ('ddl:0002_agent_reported_ansible_slots', '2026-11-04 18:09:57.187801', 'add remote_agents.reported_ansible_slots');
INSERT INTO public.schema_markers VALUES ('ddl:0003_config_route_scopes', '2026-11-04 18:09:57.221731', 'add config_mgmt_routes.scope, add config_mgmt_routes.scope_region, add config_mgmt_routes.scope_connection_id, add config_mgmt_routes.scope_workstream, drop uq_config_mgmt_route_cidr, unique (cidr, scope, scope_region, scope_connection_id, scope_workstream), split cloud-flagged routes into one row per scope');
INSERT INTO public.schema_markers VALUES ('ddl:0004_config_route_networks', '2026-11-04 18:09:57.241673', 'add config_mgmt_routes.scope_network, drop uq_config_mgmt_route_scope, unique uq_config_mgmt_route_scope_net, add agent_hosts.network');
INSERT INTO public.schema_markers VALUES ('ddl:0005_agent_lifecycle', '2026-11-04 18:09:57.259396', 'add remote_agents.control_upgrades, add remote_agents.control_updater, add remote_agents.paused, add remote_agents.upgrade_ring');
INSERT INTO public.schema_markers VALUES ('ddl:0006_agent_assurance', '2026-11-04 18:09:57.284844', 'add remote_agents.last_attest_at, add remote_agents.last_attest_ip, add remote_agents.previous_public_key, add remote_agents.previous_key_until, add remote_agents.attention, add remote_agents.reported_reattest_hours');
INSERT INTO public.schema_markers VALUES ('ddl:0007_config_route_fallback', '2026-11-04 18:09:57.290333', 'add config_mgmt_routes.fallback_to_worker');
INSERT INTO public.schema_markers VALUES ('ddl:0008_clouddb_route_ranges', '2026-11-04 18:09:57.303745', 'add cloud_databases.route_cidrs, add cloud_databases.route_network, add cloud_databases.deploy_workgroup');
INSERT INTO public.schema_markers VALUES ('ddl:0009_k8s_on_owned_agents', '2026-11-04 18:09:57.312814', 'add k8s_clusters.executor_agent_id, add agent_hosts.k8s_enabled');
INSERT INTO public.schema_markers VALUES ('ddl:0010_registered_image_recipe', '2026-11-04 18:09:57.317904', 'add registered_images.recipe');
INSERT INTO public.schema_markers VALUES ('ddl:0011_k8s_image_signature', '2026-11-04 18:09:57.323111', 'add remote_agents.control_k8s_signature');
INSERT INTO public.schema_markers VALUES ('rbac_retire_unenforced_levels_v1', '2026-11-04 18:09:58.010448', 'stripped retired levels from 0 stored map(s)');
INSERT INTO public.schema_markers VALUES ('rbac_presenter_own_pov_v1', '2026-11-04 18:09:58.01793', 'granted pov_own write+delete to 0 built-in role(s)');
INSERT INTO public.schema_markers VALUES ('rbac_presenter_own_only_v1', '2026-11-04 18:09:58.024709', 'narrowed 0 built-in presenter role(s) to own POVs');
INSERT INTO public.schema_markers VALUES ('rbac_scope_backfill_v1', '2026-11-04 18:09:58.053706', 'widened 0 row(s) with 14 new scopes');
INSERT INTO public.schema_markers VALUES ('rbac_empty_means_nothing_v1', '2026-11-04 18:09:58.061165', 'wrote the full-access map onto 0 user(s) and 0 group mapping(s) that held an empty one');


--
-- PostgreSQL database dump complete
--


