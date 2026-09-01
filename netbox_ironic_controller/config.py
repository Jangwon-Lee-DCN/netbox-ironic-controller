from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_prefix="RACKD_", extra="ignore")

    netbox_url: str = ""
    netbox_token: str = ""
    netbox_verify_tls: bool = True
    openstack_region: str = "example-region"
    openstack_auth_url: str = ""
    openstack_username: str = ""
    openstack_password: str = ""
    openstack_user_domain_name: str = "Default"
    openstack_system_scope: str = "all"
    openstack_interface: str = "internal"
    sync_enabled: bool = False
    sync_interval_seconds: int = 60
    sync_bmc_secret_namespace: str = "netbox-ironic-controller-bmc"
    sync_discovered_device_type_slug: str = "ironic-discovered-baremetal"
    sync_site_slug: str = "example-site"
    access_enabled: bool = False
    access_database_path: str = "/var/lib/baremetal-access/access.db"
    access_dcn_project_id: str = ""
    access_max_lease_days: int = 30
    access_deploy_image_ids: str = ""
    access_deploy_images_json: str = "[]"
    access_clean_steps_json: str = "[]"


@lru_cache
def get_settings() -> Settings:
    return Settings()
