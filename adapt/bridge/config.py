"""User-owned bridge connection settings; disabled until explicitly configured."""
from dataclasses import dataclass
from urllib.parse import urlsplit

from .contract import ContractError, valid_connection_id


@dataclass(frozen=True)
class BridgeConfig:
    enabled: bool = False
    connection_id: str = "bridge"
    platform: str = "wechat"
    base_url: str = "http://127.0.0.1:8770"
    token: str = ""

    @classmethod
    def from_values(cls, values: dict):
        enabled = str(values.get("BRIDGE_ENABLED") or "false").lower() in ("true", "1", "yes", "on")
        config = cls(enabled, values.get("BRIDGE_CONNECTION_ID") or "bridge", "wechat",
                     values.get("BRIDGE_BASE_URL") or cls.base_url, values.get("BRIDGE_TOKEN") or "")
        valid_connection_id(config.connection_id)
        url = urlsplit(config.base_url)
        if url.scheme not in ("http", "https") or not url.hostname or url.username \
                or url.password or url.query or url.fragment:
            raise ContractError("桥接地址必须是 HTTP(S) 地址，不能包含账号、查询参数或片段")
        if enabled and not config.token.strip():
            raise ContractError("启用通用桥接前请填写访问令牌")
        return config


def load_bridge_config() -> BridgeConfig:
    from ingress_config import settings
    return BridgeConfig.from_values(settings())
