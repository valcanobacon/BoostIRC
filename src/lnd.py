from dataclasses import dataclass
from functools import wraps

import grpc
import aiogrpc

from . import lightning_pb2 as ln
from . import lightning_pb2_grpc as lnrpc


class WalletEncryptedError(Exception):
    def __init__(self, message=None):
        message = (
            message
            or "Wallet is encrypted. Please unlock or set "
            "password if this is the first time starting lnd. "
        )
        super().__init__(message)


def handle_rpc_errors(fnc):
    """Decorator to add more context to RPC errors"""

    @wraps(fnc)
    def wrapper(*args, **kwargs):
        try:
            return fnc(*args, **kwargs)
        except grpc.RpcError as exc:
            # lnd might be active, but not possible to contact
            # using RPC if the wallet is encrypted. If we get
            # an rpc error code Unimplemented, it means that lnd is
            # running, but the RPC server is not active yet (only
            # WalletUnlocker server active) and most likely this
            # is because of an encrypted wallet.
            exc.code().value
            exc.details()
            if exc.code() == grpc.StatusCode.UNIMPLEMENTED:
                # raise WalletEncryptedError from None
                print("unimplemented")
                raise exc
            elif exc.code() == grpc.StatusCode.UNAVAILABLE:
                print("UNAVAILABLE")
                print(f"ERROR MESSAGE: {exc.details()}")
            elif (
                exc.code() == grpc.StatusCode.UNKNOWN
                and exc.details()
                == "wallet locked, unlock it to enable full RPC access"
            ):
                print("WALLET IS LOCKED!")
                raise exc
            elif exc.code() == grpc.StatusCode.UNKNOWN:
                print("unknown")
                print(f"ERROR MESSAGE: {exc.details()}")
            elif exc.code() == grpc.StatusCode.NOT_FOUND:
                print("NOT FOUND")
                print(f"ERROR MESSAGE: {exc.details()}")
            elif exc.code() == grpc.StatusCode.PERMISSION_DENIED:
                print("PERMISSION_DENIED")
                print(f"ERROR MESSAGE: {exc.details()}")
            else:
                raise exc
                return exc
        except Exception as exc:
            print("unknown exception")
            print(exc)

    return wrapper


def channel_from(host: str, port: str, cert: bytes, macaroon: bytes) -> grpc.Channel:
    def metadata_callback(_, callback):
        callback([("macaroon", macaroon)], None)

    cert_creds = grpc.ssl_channel_credentials(cert)
    auth_creds = grpc.metadata_call_credentials(metadata_callback)
    combined_creds = grpc.composite_channel_credentials(cert_creds, auth_creds)

    channel = aiogrpc.secure_channel(
        f"{host}:{port}",
        combined_creds,
        # options=[
        #     ("grpc.max_send_message_length", kwargs["max_message_length"]),
        #     ("grpc.max_receive_message_length", kwargs["max_message_length"]),
        # ],
    )

    return channel


@dataclass(frozen=True)
class lightningProvider:
    lightning_stub: lnrpc.LightningStub

    @classmethod
    def from_channel(cls, channel: grpc.Channel) -> "lightningProvider":
        return lightningProvider(
            lightning_stub=lnrpc.LightningStub(channel),
        )


@dataclass(frozen=True)
class LightningService:

    provider: lightningProvider

    @handle_rpc_errors
    async def subscribe_invoices(self, add_index=None, settle_index=None):
        """Open a stream of invoices"""
        request = ln.InvoiceSubscription(
            add_index=add_index,
            settle_index=settle_index,
        )
        async for response in self.provider.lightning_stub.SubscribeInvoices(request):
            yield response
