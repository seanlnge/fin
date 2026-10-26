"""Construct the SDK client without loading environment files or making calls."""
import plaid
from plaid.api.plaid_api import PlaidApi


def build_client(config):
    hosts = {"sandbox": plaid.Environment.Sandbox, "production": plaid.Environment.Production}
    if config.environment not in hosts:
        raise ValueError("Plaid environment must be sandbox or production")
    if not config.client_id or not config.secret:
        raise ValueError("Plaid client ID and secret are required")
    configuration = plaid.Configuration(
        host=hosts[config.environment],
        api_key={"clientId": config.client_id, "secret": config.secret},
    )
    configuration.debug = False
    return PlaidApi(plaid.ApiClient(configuration))
