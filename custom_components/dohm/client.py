"""Async BLE client for the Marpac Dohm.

Wraps the pure protocol layer with a connection and a request/response loop.
Connections go through ``bleak-retry-connector``'s ``establish_connection``,
which works both standalone and inside Home Assistant's Bluetooth stack
(including ESPHome proxies). A ``connector`` can be injected for testing.

This module imports no Home Assistant APIs. Name, clock, schedule, and
experimental ID/clock-query operations are library-only; HA uses power/speed.
Replies are matched by type (and schedule slot), not by request ID: the wire
protocol cannot distinguish a late same-type report or generic ACK from the
current reply. Callers must account for this after retries or cancellation.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from collections.abc import Awaitable, Callable
from datetime import time as wall_time
from functools import partial

from bleak.exc import BleakError

from . import protocol
from .const import CHARACTERISTIC_UUID

_LOGGER = logging.getLogger(__name__)

# Replies can lag well behind the write that triggered them. The write itself
# is ATT-acked in tens of ms, but the device requests a slow LE connection
# interval after connecting (power saving), so over BlueZ its notifications only
# drain every ~5s -- CoreBluetooth keeps the link snappy, BlueZ honors the slow
# interval. A reply that just misses a tick lands on the next one, so the wait
# must clear two cadences with margin; 5s (== one cadence) drops replies on a
# coin flip. Still well under the coordinator's 30s poll, so a truly dead device
# is reported within one cycle.
COMMAND_TIMEOUT = 15.0

# The *opening* command on a link is in a different league from the rest. On a
# freshly paired CoreBluetooth link we measured 14.67s for the first reply then
# 0.00s for every command after it. COMMAND_TIMEOUT is 15s, so the opening
# command loses that race on a coin flip -- and dropping the link over it is
# what used to cost us the bond, because the device grants a new one only in
# pairing mode (top button, ~5s). This is a property of the link, not of any
# one command: whichever command happens to go first pays it. So the opening
# command gets a longer wait and repeated tries on the *same* connection, and
# only a link that stays mute through all of them is torn down.
OPENING_TIMEOUT = 30.0
OPENING_ATTEMPTS = 3

# Bound reconnect after a deliberate unpair; the machine may have left pairing
# mode. This is separate from the long wait for an opening notification.
REBOND_RECONNECT_TIMEOUT = 30.0
REBOND_PAIR_TIMEOUT = 30.0
RECOVERY_CLEANUP_TIMEOUT = 5.0

# Client Characteristic Configuration Descriptor (notify enable bit).
CCCD_UUID = "00002902-0000-1000-8000-00805f9b34fb"


def _is_notify_acquired(err: BleakError) -> bool:
    """True for ``org.bluez.Error.NotPermitted: Notify acquired``.

    BlueZ refuses ``StartNotify`` with this when an earlier connection's notify
    subscription is still acquired (e.g. an unclean disconnect never released
    it). Matched by message so it survives bleak/BlueZ version differences.
    """
    text = str(err)
    return "Notify acquired" in text or "NotPermitted" in text


def _connection_controller(client) -> tuple[str, str] | None:
    """Identify the actual controller, not the requested BLEDevice.

    HA chooses its transport at connect time, ignoring the supplied device's
    source. No public source-pin API is available in the audited stack. These
    narrow introspection points are used only as a safety guard: if they stop
    exposing identity, disconnected recovery fails closed rather than pairing
    a different controller. They do not override HA routing.
    """
    scanner = getattr(client, "_connected_scanner", None)
    source = getattr(scanner, "source", None)
    if isinstance(source, str) and source:
        return ("ha", source)
    backend = getattr(client, "_backend", None)
    path = getattr(backend, "_device_path", None)
    if isinstance(path, str):
        match = re.fullmatch(
            r"(/org/bluez/hci[0-9]+)/dev_(?:[0-9A-Fa-f]{2}_){5}[0-9A-Fa-f]{2}", path
        )
        if match:
            return ("bluez", match[1])
    return None


class DohmError(Exception):
    """Base error for Dohm operations."""


class DohmCommandError(DohmError):
    """The device rejected a command (replied ``Failed NN$``)."""


class DohmProtocolError(DohmError):
    """A notification is malformed or is an unsupported command echo."""


class DohmRebondUnsupported(DohmError):
    """The Bluetooth backend can't clear a stored bond.

    Raised for CoreBluetooth (macOS has no unpair API) and for ESPHome
    firmware older than 2024.3.0, which doesn't advertise the ``PAIRING``
    feature flag. BlueZ supports unpairing, but it also disconnects the device.
    """


async def _default_connector(ble_device, *, ble_device_callback=None):
    """Connect without explicitly pairing, as in the working 0.3.0 path.

    Android's createBond() call is not a portable requirement for an existing
    link. Let the backend reuse its bond or handle authentication implicitly;
    explicit unpair/pair belongs only to deliberate rebond() recovery.

    Connecting is racy on this device -- the first attempt often comes back
    ``le-connection-abort-by-local`` -- so ``establish_connection`` retrying is
    essential. The synchronous ``ble_device_callback`` is forwarded for API
    compatibility, not treated as a rediscovery guarantee: the audited retry
    connector accepts it without using it. HA's client wrapper itself chooses
    from its current scanners on each connect attempt.
    """
    from bleak_retry_connector import BleakClientWithServiceCache, establish_connection

    async def establish():
        return await establish_connection(
            BleakClientWithServiceCache,
            ble_device,
            ble_device.address,
            ble_device_callback=ble_device_callback,
        )

    client = await establish()
    # A connection cut short (e.g. the device's brief connectable window) can
    # leave a partial service cache that hides the command characteristic. If
    # it's missing, clear the cache and rediscover once.
    if client.services.get_characteristic(CHARACTERISTIC_UUID) is None:
        await client.clear_cache()
        await client.disconnect()
        client = await establish()
    return client


class DohmClient:
    def __init__(
        self,
        ble_device,
        *,
        device_id: str | None = None,
        connector: Callable[[object], Awaitable[object]] | None = None,
        ble_device_callback: Callable[[], object] | None = None,
    ) -> None:
        self._ble_device = ble_device
        # Bound into the default connector rather than passed through the
        # connector signature, so an injected test connector stays one-argument.
        self._connector = connector or partial(
            _default_connector, ble_device_callback=ble_device_callback
        )
        self._client = None
        self._notifying = False
        # HA supplies its hardware-observed MAC-derived ID to avoid an opening
        # round trip. Standalone callers can omit it (including on macOS, where
        # the address is a host UUID); connect then asks the device once.
        if device_id is not None:
            protocol.query_name(device_id)  # validate without touching hardware
        self._device_id = device_id
        # Has anything on this link actually answered yet? Until it has, every
        # command is the opening command and gets OPENING_TIMEOUT.
        self._opened = False
        self._queue: asyncio.Queue[bytes] = asyncio.Queue()
        self._lock = asyncio.Lock()
        self._recovering = False

    @property
    def device_id(self) -> str | None:
        return self._device_id

    @property
    def is_connected(self) -> bool:
        return self._client is not None and self._client.is_connected

    async def connect(self, ble_device=None) -> None:
        if self._recovering:
            raise DohmError("pairing recovery is in progress")
        async with self._lock:
            if self._recovering:
                raise DohmError("pairing recovery is in progress")
            if self.is_connected:
                if self._device_id is None:
                    await self._get_device_id()
                return
            await self._connect(ble_device)

    async def _connect(self, ble_device=None) -> None:
        # In Home Assistant the BLEDevice can change between connections, so
        # allow refreshing it on (re)connect.
        if ble_device is not None:
            self._ble_device = ble_device
        _LOGGER.debug("connecting to %s", getattr(self._ble_device, "address", "?"))
        self._client = await self._connector(self._ble_device)
        # Once connected, any later failure must not leak a connected, notifying
        # client: the held link keeps BlueZ's notify subscription acquired, so
        # the next start_notify is refused with NotPermitted: Notify acquired.
        # Release it before propagating. _drop() is best-effort cleanup.
        try:
            self._opened = False
            await self._subscribe()
            if self._device_id is None:
                await self._get_device_id()
            _LOGGER.debug("connected; device id %s", self._device_id)
        except BaseException:
            await self._drop()
            raise

    async def _subscribe(self) -> None:
        try:
            await self._client.start_notify(CHARACTERISTIC_UUID, self._on_notify)
        except BleakError as err:
            if not _is_notify_acquired(err):
                raise
            # An earlier connection left BlueZ's notify subscription acquired and
            # our graceful disconnect never ran to release it (crash, dropped
            # link, integration reload). A full disconnect drops the device
            # connection and frees it; reconnect once and subscribe again.
            try:
                await self._client.stop_notify(CHARACTERISTIC_UUID)
            except Exception as err:  # noqa: BLE001 - best-effort; link may be gone
                _LOGGER.debug("stop notify before recovery failed: %s", err)
            await self._client.disconnect()
            self._client = await self._connector(self._ble_device)
            await self._client.start_notify(CHARACTERISTIC_UUID, self._on_notify)
        self._notifying = True

    async def _drop(self) -> None:
        """Best-effort teardown. The caller already has the failure that matters."""
        try:
            await self.disconnect()
        except Exception as err:  # noqa: BLE001 - best-effort; keep the real cause
            _LOGGER.debug("transport cleanup failed: %s", err)

    async def disconnect(self) -> None:
        client = self._client
        self._client = None
        self._opened = False
        if client is None:
            return
        _LOGGER.debug("disconnecting (notifying=%s)", self._notifying)
        # Release BlueZ's per-characteristic notify subscription before dropping
        # the link. Skipping this leaves it "acquired", so the next connect's
        # start_notify fails with org.bluez.Error.NotPermitted: Notify acquired,
        # piling up HCI resources until ENOMEM. The link may already be gone, so
        # failing to stop notify must not prevent the disconnect.
        try:
            if self._notifying:
                try:
                    async with asyncio.timeout(RECOVERY_CLEANUP_TIMEOUT):
                        await client.stop_notify(CHARACTERISTIC_UUID)
                except Exception as err:  # noqa: BLE001 - best-effort cleanup
                    _LOGGER.debug("stop notify during disconnect failed: %s", err)
                finally:
                    self._notifying = False
        finally:
            # Cancellation during stop_notify must still release the single
            # connection slot. Bound this separately from notification cleanup.
            async with asyncio.timeout(RECOVERY_CLEANUP_TIMEOUT):
                await client.disconnect()

    async def rebond(self, ble_device=None) -> str:
        """Deliberately clear one controller's bond and request a new one.

        Silence alone is not proof of a stale key. Only a user's recovery action
        calls this. ESPHome can pair on the still-connected link; BlueZ removes
        its device object during unpair and must connect/discover again first.
        A reconnect may route elsewhere in HA, so verify the actual controller
        before pairing. Never clear a second controller automatically.
        """
        if self._recovering:
            raise DohmError("pairing recovery is already in progress")
        self._recovering = True
        try:
            async with self._lock:
                await self.disconnect()
                if ble_device is not None:
                    self._ble_device = ble_device
                return await self._rebond()
        finally:
            self._recovering = False

    async def _rebond(self) -> str:
        client = await self._connector(self._ble_device)
        try:
            unpair = getattr(client, "unpair", None)
            pair = getattr(client, "pair", None)
            if unpair is None or pair is None:
                raise DohmRebondUnsupported(
                    f"{type(client).__name__} exposes no unpair/pair"
                )
            controller = _connection_controller(client)
            _LOGGER.debug("pairing recovery selected controller %s", controller)
            try:
                await unpair()
            except NotImplementedError as err:
                raise DohmRebondUnsupported(str(err) or type(client).__name__) from err
            try:
                if not client.is_connected:
                    if controller is None:
                        raise DohmError(
                            "cannot verify the original Bluetooth controller; "
                            "retry normal setup after rediscovery"
                        )
                    # Reuse the public client, whose native BlueZ backend reset
                    # its device path in unpair(). connect() performs discovery
                    # again, without requesting pairing before the controller check.
                    from bleak_retry_connector import retry_bluetooth_connection_error

                    reconnect = retry_bluetooth_connection_error(attempts=3)(
                        client.connect
                    )
                    async with asyncio.timeout(REBOND_RECONNECT_TIMEOUT):
                        await reconnect()
                    if not client.is_connected:
                        raise DohmError("Bluetooth reconnect did not establish a link")
                    reconnected_controller = _connection_controller(client)
                    if reconnected_controller != controller:
                        raise DohmError(
                            f"Bluetooth controller changed during recovery "
                            f"({controller!r} -> {reconnected_controller!r}); "
                            "no pairing requested on the replacement controller"
                        )
                # Like Android's saved-device path: connect, then request a bond.
                # Use a bound for proxies that never return a pairing response.
                async with asyncio.timeout(REBOND_PAIR_TIMEOUT):
                    await pair()
            except Exception as err:  # noqa: BLE001 - report partial recovery
                return (
                    f"cleared the stored bond; re-pair failed "
                    f"({type(err).__name__}: {err}) -- hold the top button ~5s"
                )
            return "cleared the stored bond and paired again"
        finally:
            try:
                async with asyncio.timeout(RECOVERY_CLEANUP_TIMEOUT):
                    await client.disconnect()
            except Exception as err:  # noqa: BLE001 - preserve recovery outcome
                _LOGGER.debug("cleanup after pairing recovery: %s", err)

    def _on_notify(self, _sender, data: bytearray) -> None:
        self._queue.put_nowait(bytes(data))

    async def _rearm_notify(self) -> None:
        # The Dohm goes notify-deaf on a long-lived link until its CCCD is
        # re-enabled: the device stops emitting notifications even though writes
        # are still ATT-acked, so command replies silently go missing. The
        # iOS capture rewrites 01 00 to the CCCD before commands; retain that
        # workaround. Android 1.0.7.0 writes it only during connection setup.
        # Best-effort -- some backends (e.g. CoreBluetooth) hide the CCCD,
        # where the start_notify subscription is the only handle we have.
        char = self._client.services.get_characteristic(CHARACTERISTIC_UUID)
        cccd = char.get_descriptor(CCCD_UUID) if char is not None else None
        if cccd is None:
            # Worth logging every time: if the backend in use hides the CCCD,
            # the v0.1.6 re-arm is silently a no-op and cannot be what keeps
            # replies flowing (or what fails to).
            _LOGGER.debug("no CCCD exposed for %s; re-arm skipped", CHARACTERISTIC_UUID)
            return
        try:
            await self._client.write_gatt_descriptor(cccd.handle, b"\x01\x00")
        except Exception as err:  # noqa: BLE001 - best-effort; may already be armed
            _LOGGER.debug("CCCD re-arm failed on handle 0x%04x: %s", cccd.handle, err)

    def _check_ready(self) -> None:
        if self._recovering:
            raise DohmError("pairing recovery is in progress")
        if not self.is_connected:
            raise DohmError("not connected; call connect() first")

    def _require_device_id(self) -> str:
        if self._device_id is None:
            raise DohmError("device ID is unknown; call connect() first")
        return self._device_id

    async def _command[T](
        self,
        build_payload: Callable[[str], bytes],
        reply_type: type[T],
        *,
        matches: Callable[[T], bool] | None = None,
    ) -> T:
        """Build/send under the lock so a concurrent ID change cannot stale it."""
        self._check_ready()
        async with self._lock:
            self._check_ready()
            payload = build_payload(self._require_device_id())
            return await self._request(payload, reply_type, matches=matches)

    async def _request[T](
        self,
        payload: bytes,
        reply_type: type[T],
        *,
        matches: Callable[[T], bool] | None = None,
    ) -> T:
        """Exchange a typed reply. The caller must hold the operation lock."""

        def accepts(raw: bytes) -> bool:
            try:
                message = protocol.parse(raw)
            except ValueError as err:
                raise DohmProtocolError(
                    f"invalid reply {raw!r} to {payload!r}"
                ) from err
            return isinstance(message, protocol.Failure) or (
                isinstance(message, reply_type)
                and (matches is None or matches(message))
            )

        raw = await self._exchange(payload, accepts)
        message = protocol.parse(raw)
        if isinstance(message, protocol.Failure):
            raise DohmCommandError(f"device rejected {payload!r}: {message.code}")
        return message

    async def _exchange(
        self, payload: bytes, accepts: Callable[[bytes], bool]
    ) -> bytes:
        """One request/reply, with the opening command handled as its own case."""
        if self._opened:
            return await self._send(
                payload, COMMAND_TIMEOUT, accepts=accepts, drop_on_failure=True
            )

        last_err: TimeoutError | None = None
        for attempt in range(1, OPENING_ATTEMPTS + 1):
            try:
                raw = await self._send(
                    payload, OPENING_TIMEOUT, accepts=accepts, drop_on_failure=False
                )
            except TimeoutError as err:
                # Silence on a fresh link is not evidence the link is bad; it is
                # what a slow first notify looks like. Ask again on the same
                # connection rather than dropping a bond we may never get back.
                last_err = err
                _LOGGER.debug(
                    "opening command %r unanswered (attempt %d/%d)",
                    payload,
                    attempt,
                    OPENING_ATTEMPTS,
                )
            except Exception:
                # Not a timeout: the link itself is bad, so stop retrying on it.
                # Cancellation is not caught here -- it says nothing about the
                # link, and tearing one down over it would be wrong.
                await self._drop()
                raise
            else:
                self._opened = True
                return raw

        # Mute through every attempt: connected, ATT-acking, and notify-deaf.
        # Nothing here reconnects on its own, so the link has to go or the
        # coordinator retries this same dead client every poll, forever.
        _LOGGER.debug("link never answered %r; dropping it", payload)
        await self._drop()
        raise last_err

    def _take_matching_queued(self, accepts: Callable[[bytes], bool]) -> bytes | None:
        """Salvage a matching late frame without extending the reply deadline."""
        for _ in range(self._queue.qsize()):
            raw = self._queue.get_nowait()
            if accepts(raw):
                return raw
        return None

    async def _send(
        self,
        payload: bytes,
        timeout: float,
        *,
        accepts: Callable[[bytes], bool],
        drop_on_failure: bool,
    ) -> bytes:
        await self._rearm_notify()
        while not self._queue.empty():
            self._queue.get_nowait()
        started = time.monotonic()
        try:
            await self._client.write_gatt_char(
                CHARACTERISTIC_UUID, payload, response=True
            )
            acked = time.monotonic()
            deadline = time.monotonic() + timeout
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raw = self._take_matching_queued(accepts)
                    if raw is None:
                        raise TimeoutError(f"no matching reply to {payload!r}")
                    break
                try:
                    raw = await asyncio.wait_for(self._queue.get(), remaining)
                except TimeoutError:
                    # Drain a bounded snapshot: a valid late reply can sit
                    # behind an unrelated report when wait_for loses the race.
                    raw = self._take_matching_queued(accepts)
                    if raw is None:
                        raise
                    break
                if accepts(raw):
                    break
                _LOGGER.debug("ignoring unrelated reply %r to %r", raw, payload)
            # The ack/reply split is the diagnostic that matters: a device that
            # acks fast and then never answers has gone notify-deaf, while a
            # slow ack means the link itself is struggling.
            _LOGGER.debug(
                "%r -> %r (ack %.2fs, reply %.2fs)",
                payload,
                bytes(raw),
                acked - started,
                time.monotonic() - acked,
            )
        except Exception as err:
            # Cancellation is deliberately not in here: it is not evidence of a
            # bad link, and dropping one over it would cost a reconnect.
            _LOGGER.debug(
                "%r failed after %.2fs (%s: %s)",
                payload,
                time.monotonic() - started,
                type(err).__name__,
                err,
            )
            if drop_on_failure:
                await self._drop()
            raise
        return raw

    async def _get_device_id(self) -> str:
        report = await self._request(protocol.query_id(), protocol.DeviceId)
        self._device_id = report.value
        return report.value

    async def get_device_id(self) -> str:
        """Query and retain the actual ID; never change the device's ID."""
        self._check_ready()
        async with self._lock:
            self._check_ready()
            return await self._get_device_id()

    async def set_speed(self, speed: int) -> None:
        await self._command(
            lambda device_id: protocol.set_speed(device_id, speed), protocol.Ack
        )

    async def get_speed(self) -> int:
        return (await self._command(protocol.query_speed, protocol.SpeedReport)).speed

    async def set_power(self, on: bool) -> None:
        await self._command(
            lambda device_id: protocol.set_power(device_id, on), protocol.Ack
        )

    async def get_power(self) -> bool:
        return (await self._command(protocol.query_power, protocol.PowerReport)).on

    async def get_name(self) -> str:
        return (await self._command(protocol.query_name, protocol.NameReport)).name

    async def set_name(self, name: str) -> None:
        """Set the device's own name, independently of any HA entity name."""
        await self._command(
            lambda device_id: protocol.set_name(device_id, name), protocol.Ack
        )

    async def set_time(self, value: wall_time, weekday: int) -> None:
        """Set local HHMM and ISO weekday (Monday=1 … Sunday=7).

        Seconds are discarded. No automatic sync occurs on connect: changing
        the clock can change when existing onboard schedules operate.
        """
        await self._command(
            lambda device_id: protocol.set_time(device_id, value, weekday), protocol.Ack
        )

    async def get_time(self) -> bytes:
        """Experimental clock query; return a raw T,...$ reply, not a guessed schema.

        Only the command letter is declared by the APK. Query parameters,
        firmware support and reply format remain unverified. An echo or malformed
        frame raises DohmProtocolError; a rejection raises DohmCommandError.
        """
        self._check_ready()
        async with self._lock:
            self._check_ready()
            payload = protocol.query_time(self._require_device_id())

            def accepts(raw: bytes) -> bool:
                try:
                    text = raw.decode()
                    if not text.endswith("$") or "$" in text[:-1]:
                        raise ValueError("invalid frame")
                    # Preserve unknown clock fields verbatim. Matching the
                    # uppercase reply letter is inferred from other queries.
                    if text.startswith("T,"):
                        return True
                    message = protocol.parse(raw)
                except ValueError as err:
                    raise DohmProtocolError(
                        f"invalid experimental clock reply {raw!r}"
                    ) from err
                return isinstance(message, protocol.Failure)

            raw = await self._exchange(payload, accepts)
            if raw.startswith(b"Failed"):
                failure = protocol.parse(raw)
                raise DohmCommandError(f"device rejected {payload!r}: {failure.code}")
            return raw

    async def set_device_id(self, new_device_id: str) -> str:
        """Experimental ID write using inferred syntax, then query the actual ID.

        This can break the observed MAC-to-ID relation. After attempting the
        write, the cached ID stays unknown until readback succeeds, even on
        timeout/cancellation. No assumption about persistence or rollback is made.
        HA never calls this method; changing IDs can invalidate its derived ID.
        """
        self._check_ready()
        async with self._lock:
            self._check_ready()
            payload = protocol.set_device_id(self._require_device_id(), new_device_id)
            self._device_id = None
            await self._request(payload, protocol.Ack)
            actual = await self._get_device_id()
            if actual != new_device_id:
                raise DohmError(
                    f"device ID readback mismatch: expected {new_device_id!r}, "
                    f"got {actual!r}"
                )
            return actual

    async def get_schedule(self, slot: int) -> protocol.ScheduleReport:
        return await self._command(
            lambda device_id: protocol.query_schedule(device_id, slot),
            protocol.ScheduleReport,
            matches=lambda report: report.slot == slot,
        )

    async def get_schedules(self) -> tuple[protocol.ScheduleReport, ...]:
        """Read slots 1–3 without interleaving other client operations."""
        self._check_ready()
        async with self._lock:
            self._check_ready()
            device_id = self._require_device_id()
            reports = []
            for slot in range(1, 4):
                reports.append(
                    await self._request(
                        protocol.query_schedule(device_id, slot),
                        protocol.ScheduleReport,
                        matches=lambda report, slot=slot: report.slot == slot,
                    )
                )
            return tuple(reports)

    async def set_schedule(
        self,
        slot: int,
        start: wall_time,
        stop: wall_time,
        days: int,
        speed: int,
    ) -> protocol.ScheduleReport:
        """Write a slot's three fields, then verify and return its readback.

        Monday is bit 0 of days; Sunday is bit 6. days=0 disables the slot;
        speed=0 deletes it (app-derived, not yet hardware-verified). Seconds
        are discarded. The operation is serialized, not firmware-atomic:
        failure/cancellation can leave a partial update. No rollback or bond
        clearing is attempted. Save originals before making deliberate changes.
        """
        self._check_ready()
        async with self._lock:
            self._check_ready()
            device_id = self._require_device_id()
            # Validate every field before the first write.
            payloads = protocol.set_schedule(device_id, slot, start, stop, days, speed)
            expected = protocol.ScheduleReport(
                slot,
                wall_time(start.hour, start.minute),
                wall_time(stop.hour, stop.minute),
                days,
                speed,
            )
            for payload in payloads:
                await self._request(payload, protocol.Ack)
            report = await self._request(
                protocol.query_schedule(device_id, slot),
                protocol.ScheduleReport,
                matches=lambda report: report.slot == slot,
            )
            if report != expected:
                raise DohmError(
                    f"schedule slot {slot} readback mismatch: "
                    f"expected {expected!r}, got {report!r}; slot may be partially updated"
                )
            return report
