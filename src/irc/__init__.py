import asyncio
import codecs
import itertools
import json
import logging
import os
from collections import defaultdict
from datetime import datetime, timedelta
from enum import Enum
from typing import List

import bottom
import click

from ..lnd import LightningService, channel_from, lightningProvider
from ..numerology import number_to_numerology

logging.getLogger().setLevel(logging.INFO)


def read_macaroon(filename):
    with open(filename, "rb") as file_:
        return codecs.encode(file_.read(), "hex")


def read_tlscert(filename):
    with open(filename, "rb") as file_:
        return file_.read()


class ChannelMapType(Enum):
    Podcast = "podcast"
    FeedId = "feedId"
    Url = "url"
    Guid = "guid"


@click.command()
@click.option("--lnd-host", default="127.0.0.1")
@click.option("--lnd-port", type=click.IntRange(0), default=10009)
@click.option(
    "--lnd-macaroon", type=click.Path(exists=True), default="readonly.macaroon"
)
@click.option("--lnd-tlscert", type=click.Path(exists=True), default="tls.cert")
@click.option("--irc-host", default="irc.zeronode.net")
@click.option("--irc-port", type=int, default=6697)
@click.option("--irc-ssl", type=bool, default=True)
@click.option("--irc-password")
@click.option("--irc-nick", default="boostirc")
@click.option("--irc-channel", default=["#boostirc"], multiple=True)
@click.option("--irc-channel-map", type=(str, ChannelMapType, str), multiple=True)
@click.option("--irc-realname", default="Boost IRC Bot")
@click.option("--irc-nick-password")
@click.option("--minimum-donation", type=int)
@click.option("--allowed-name", multiple=True)
@click.option("--verbose/--no-verbose", default=False)
@click.pass_context
def cli(
    ctx,
    lnd_host,
    lnd_port,
    lnd_macaroon,
    lnd_tlscert,
    irc_host,
    irc_port,
    irc_ssl,
    irc_password,
    irc_nick,
    irc_nick_password,
    irc_channel,
    irc_channel_map,
    irc_realname,
    minimum_donation,
    allowed_name,
    verbose,
):
    ctx.ensure_object(dict)

    if verbose:
        logging.getLogger().setLevel(logging.DEBUG)

    channel_map = None
    if irc_channel_map:
        channel_map = defaultdict(lambda: defaultdict(set))
        for channel, channel_map_type, value in irc_channel_map:
            channel_map[channel_map_type][value.lower().strip()].add(
                channel.lower().strip()
            )
        logging.debug(channel_map)

    # from: https://github.com/lightningnetwork/lnd/blob/master/docs/grpc/python.md
    # Due to updated ECDSA generated tls.cert we need to let gprc know that
    # we need to use that cipher suite otherwise there will be a handhsake
    # error when we communicate with the lnd rpc server.
    os.environ["GRPC_SSL_CIPHER_SUITES"] = "HIGH+ECDSA"

    lnd_client = LightningService(
        provider=lightningProvider.from_channel(
            channel_from(
                host=lnd_host,
                port=lnd_port,
                cert=read_tlscert(filename=lnd_tlscert),
                macaroon=read_macaroon(filename=lnd_macaroon),
            )
        )
    )

    bot = bottom.Client(host=irc_host, port=irc_port, ssl=irc_ssl)

    @bot.on("CLIENT_CONNECT")
    async def connect(**kwargs):
        bot.send("NICK", nick=irc_nick)
        bot.send("USER", user=irc_nick, realname=irc_realname)
        if irc_password is not None:
            bot.send("PASS", password=irc_password)

        # Don't try to join channels until the server has
        # sent the MOTD, or signaled that there's no MOTD.
        _, pending = await asyncio.wait(
            [
                bot.wait("RPL_ENDOFMOTD"),
                bot.wait("ERR_NOMOTD"),
            ],
            return_when=asyncio.FIRST_COMPLETED,
        )

        # Cancel whichever waiter's event didn't come in.
        for future in pending:
            future.cancel()

        if irc_nick_password:
            bot.send(
                "PRIVMSG", target="NickServ", message=f"IDENTIFY {irc_nick_password}"
            )
            logging.debug(f"Identified with NickServ at {datetime.now().isoformat()}")

        for channel in irc_channel:
            bot.send("JOIN", channel=channel)
            logging.debug(f"Joined channel {channel} at {datetime.now().isoformat()}")

        for channel, _, _ in irc_channel_map:
            bot.send("JOIN", channel=channel)
            logging.debug(
                f"Joined mapped channel {channel} at {datetime.now().isoformat()}"
            )

    @bot.on("CLIENT_DISCONNECT")
    async def reconnect(**kwargs):
        await asyncio.sleep(2, loop=bot.loop)
        bot.loop.create_task(bot.connect())
        logging.info(f"Reconnected at {datetime.now().isoformat()}")

    @bot.on("PING")
    def keepalive(message, **kwargs):
        logging.debug(f"ping at {datetime.now().isoformat()}")
        bot.send("PONG", message=message)
        logging.debug(f"pong at {datetime.now().isoformat()}")

    async def subscribe_invoices():
        invoices = lnd_client.subscribe_invoices()
        print(invoices)
        async for invoice in invoices:
            for tlv in invoice.htlcs:
                try:
                    data = tlv.custom_records.get(7629169)
                    if data is None:
                        continue

                    data = json.loads(data)

                    if allowed_name:
                        name = data.get("name")
                        if not name:
                            continue

                        if name.lower() not in [x.lower() for x in allowed_name]:
                            continue

                    if "action" not in data or str(data["action"]).lower() != "boost":
                        continue

                    value = int(data.get("value_msat_total", 0)) // 1000
                    if not value:
                        value = invoice.value

                    if minimum_donation is not None and value < minimum_donation:
                        logging.debug("Donation too low, skipping", data)
                        continue

                    fullmessage = _new_message(data, value)
                    logging.debug(fullmessage)

                    # arbitrarily slice to chunks of 250 in an attempt
                    # to accomodate long bot nicks/hostmasks, or channels
                    message = _chunks(fullmessage, 250)

                    for chunk in message:
                        channels = (
                            set(
                                itertools.chain(
                                    channel_map[ChannelMapType.Podcast].get(
                                        str(data.get("podcast", "")).lower().strip(), []
                                    ),
                                    channel_map[ChannelMapType.FeedId].get(
                                        str(data.get("feedID", "")).lower().strip(), []
                                    ),
                                    channel_map[ChannelMapType.Url].get(
                                        str(data.get("url", "")).lower().strip(), []
                                    ),
                                    channel_map[ChannelMapType.Guid].get(
                                        str(data.get("guid", "")).lower().strip(), []
                                    ),
                                )
                            )
                            if channel_map is not None
                            else set()
                        )
                        # Always post to all channels in irc_channel
                        channels.update(irc_channel)

                        for channel in channels:
                            bot.send("PRIVMSG", target=channel, message=chunk)

                except Exception as exception:
                    logging.exception(exception)

    bot.loop.run_until_complete(bot.connect())

    bot.loop.run_until_complete(subscribe_invoices())

    bot.loop.run_forever()


def _get(data, key, format_found=None, default=None):
    if key in data:
        value = data[key]
        if value:
            if format_found is None:
                return value
            if callable(format_found):
                return format_found(key, value)
            return format_found.format(**{key: value})
    return default


def _sanitize(message):
    # for IRC TODO: check for and replace more than just newlines
    if isinstance(message, str):
        if r"\\u" in repr(message):
            message = (
                message.encode("ascii")
                .decode("unicode-escape")
                .encode("utf-16", "surrogatepass")
                .decode("utf-16")
            )
        return message.replace("\n", "")
    return message


def _new_message(data, value, numerology_func=number_to_numerology):

    amount = f"\x02\u200b{value}\x02 sats"
    app = _sanitize(_get(data, "app_name", "via {app_name}"))
    sender = _sanitize(_get(data, "sender_name", default="Anonymous"))
    podcast = _sanitize(_get(data, "podcast", "\x02[{podcast}]\x02"))
    episode = _sanitize(_get(data, "episode", "\x02[{episode}]\x02"))
    message = _sanitize(_get(data, "message", 'saying "\x02{message}\x02"'))
    timestamp = _get(data, "ts", lambda _, v: "@{}".format(timedelta(seconds=int(v))))
    numerology = numerology_func(value)

    # Build the data to send to Matrix
    data = ""
    if numerology:
        data += numerology + " "
    if podcast:
        data += podcast + " "
    if episode:
        data += episode + " "
    if sender:
        data += sender + " "
    data += f"boosted {amount}"
    if message:
        data += " " + message
    if timestamp:
        data += " " + timestamp
    if app:
        data += " " + app

    return data


def _chunks(message: str, n: int):
    n = max(1, n)
    for i in range(0, len(message), n):
        yield message[i : i + n]
