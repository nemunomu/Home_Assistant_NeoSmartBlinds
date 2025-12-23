import logging
import time
from datetime import datetime

import asyncio

from .const import (
    CMD_UP,
    CMD_UP2,
    CMD_DOWN,
    CMD_DOWN2,
    CMD_STOP,
    CMD_FAV,
    CMD_FAV_2,
    CMD_MICRO_UP,
    CMD_MICRO_DOWN,
    CMD_MICRO_UP2,
    CMD_MICRO_DOWN2,
    DEFAULT_IO_TIMEOUT,
    DEFAULT_COMMAND_AGGREGATION_PERIOD,
    DEFAULT_COMMAND_BACKOFF
)

_LOGGER = logging.getLogger(__name__)
LOGGER = logging.getLogger()

parents = {}

class NeoParentBlind(object):
    def __init__(self):
        self._child_count = 0
        self._intent = 0
        self._fulfilled = False
        self._time_of_first_intent = None
        self._intended_command = None
        self._wait = asyncio.Event()
        self._wait_done = asyncio.Event()
        self._lock_command_runner = False

    def add_child(self):
        self._child_count += 1

    def register_intent(self, command):
        if self._intended_command is None:
            self._intended_command = command

        if self._intended_command != command:
            _LOGGER.debug("{}, abandoning aggregated group command {} with {} waiting".format(
                command, self._intended_command, self._intent)
                )
            self._wait.set()
            return False

        self._intent += 1
        if self._intent >= self._child_count:
            self._fulfilled = True
            self._wait.set()
        
        if self._time_of_first_intent is None:
            self._time_of_first_intent = time.time() + DEFAULT_COMMAND_AGGREGATION_PERIOD

        return True


    def unregister_intent(self):
        self._intent -= 1
        if self._intent == 0:
            self._fulfilled = False
            self._time_of_first_intent = None
            self._intended_command = None
            self._wait.clear()
            self._wait_done.clear()
            self._lock_command_runner = False

    CHANGE_DEVICE = 0
    IGNORE = 1
    USE_DEVICE = 2

    def done(self):
        self._wait_done.set()

    def fulfilled(self):
        return self._fulfilled

    def act_on_intent(self):
        if not self._fulfilled:
            return NeoParentBlind.USE_DEVICE
        
        if self._intent == self._child_count and not self._lock_command_runner:
            self._lock_command_runner = True
            return NeoParentBlind.CHANGE_DEVICE

        return NeoParentBlind.IGNORE

    async def async_wait_done(self):
        await asyncio.create_task(self._wait_done.wait())

    async def async_backoff(self):
        now = time.time()
        sleep_duration = self._time_of_first_intent - now if self._time_of_first_intent is not None else 0
        if sleep_duration > 0:
            _LOGGER.debug("{}, observing blind commands for {:.3f}s".format(self._intended_command, sleep_duration))
            try:
                await asyncio.wait_for(asyncio.create_task(self._wait.wait()), sleep_duration)
            except asyncio.TimeoutError:
                # all done
                pass 

time_of_last_command = time.time()

async def async_backoff():
    global time_of_last_command
    now = time.time()
    sleep_duration = 0.0
    since_last = now - time_of_last_command

    if since_last < DEFAULT_COMMAND_BACKOFF:
        sleep_duration = DEFAULT_COMMAND_BACKOFF - since_last

    time_of_last_command = now + sleep_duration

    if sleep_duration > 0.0:
        _LOGGER.debug("Delaying command for {:.3f}s".format(sleep_duration))
        await asyncio.sleep(sleep_duration)

class NeoCommandSender(object):
    def __init__(self, host, the_id, device, port, motor_code):
        self._host = host
        self._port = port
        self._the_id = the_id
        self._device = device
        self._motor_code = motor_code
        self._was_connected = None

    def on_io_complete(self, result=None):
        """
        Helper function to trap connection status and log it on change only.
        result is either None (success) or an exception (fail)
        """
        if result is None:
            if not self._was_connected:
                _LOGGER.info('{}, connected to hub'.format(self._device))
                self._was_connected = True
        else:
            if self._was_connected or self._was_connected is None:
                _LOGGER.warning('{}, disconnected from hub: {}'.format(self._device, repr(result)))
                self._was_connected = False
        
        return self._was_connected

    @property
    def device(self):
        return self._device

    @property
    def motor_code(self):
        return self._motor_code


    def register_parents(self, device):
        global parents

        if not device in parents:
            parent = NeoParentBlind()
            parents[device] = parent

        parents[device].add_child()

    async def async_send_command(self, command, parent_device = None):
        global parents
        action = NeoParentBlind.USE_DEVICE
        intent_registered = False

        if parent_device is not None:
            # find parent, register intent
            if parent_device in parents:
                _LOGGER.debug("{}, checking for aggregation {}, command {}".format(self._device, parent_device, command))
                parent = parents[parent_device]
                intent_registered = parent.register_intent(command)
                if intent_registered:
                    await parent.async_backoff()
                    action = parent.act_on_intent()

        # if parent fulfilled, use it else continue
        if action == NeoParentBlind.USE_DEVICE:
            _LOGGER.debug("{}, issuing command, {}".format(self._device, command))
            await async_backoff()
            if intent_registered:
                parent.unregister_intent()
            return await self.async_send_command_to_device(command, self._device)
        elif action == NeoParentBlind.CHANGE_DEVICE:
            _LOGGER.debug("{}, issuing to group command ({}) instead {}".format(self._device, command, parent_device))
            await async_backoff()
            result = await self.async_send_command_to_device(command, parent_device)
            if intent_registered:
                parent.done()
                parent.unregister_intent()
            return result
        else:
            _LOGGER.debug("{}, aggregated to group command, {}".format(self._device, command))
            if intent_registered:
                await parent.async_wait_done()
                parent.unregister_intent()
            return True

    async def async_send_command_to_device(self, command, device):
        _LOGGER.error("I should do something but I cannot!")

class NeoTcpCommandSender(NeoCommandSender):
    
    async def async_send_command_to_device(self, command, device):
        """Command sender for TCP"""

        async def async_sender():
            """
            Wrap all the IO in an awaitable closure so a timeout can be put on it in the outer function
            """
            reader, writer = await asyncio.open_connection(self._host, self._port)

            mc = ""
            if self._motor_code:
                mc = "!{}".format(self._motor_code)

            complete_command = device + "-" + command + mc + '\r\n'
            _LOGGER.debug("{}, Tx: {}".format(device, complete_command))
            writer.write(complete_command.encode())
            await writer.drain()

            response = await reader.read()
            _LOGGER.debug("{}, Rx: {}".format(device, response.decode()))

            writer.close()
            await writer.wait_closed()

        try:
            await asyncio.wait_for(async_sender(), timeout=DEFAULT_IO_TIMEOUT)
            return self.on_io_complete()
        except Exception as e:
            return self.on_io_complete(e)

class NeoHttpCommandSender(NeoCommandSender):
    def __init__(self, http_session_factory, host, the_id, device, port, motor_code):
        self._session = http_session_factory(DEFAULT_IO_TIMEOUT)
        super().__init__(host, the_id, device, port, motor_code)

    async def async_send_command_to_device(self, command, device):
        """Command sender for HTTP"""
        url = "http://{}:{}/neo/v1/transmit".format(self._host, self._port)

        mc = ""
        if self._motor_code:
            mc = "!{}".format(self._motor_code)

        hash_string = str(datetime.now().microsecond).zfill(7)
        # hash_string = pre_strip[-7:].strip(".")

        params = {'id': self._the_id, 'command': device + "-" + command + mc, 'hash': hash_string}

        try:
            async with self._session.get(url=url, params=params, raise_for_status=True) as r:
                _LOGGER.debug("{}, Tx: {}".format(device, r.url))
                _LOGGER.debug("{}, Rx: {} - {}".format(device, r.status, await r.text()))
                return self.on_io_complete()
        except Exception as e:
            # LOGGER.error("Exception while sending http command: {}, Tx: {}".format(self._device, r.url))
            _LOGGER.error("Parameters: {}".format(params))
            _LOGGER.error("URL: {}".format(url))
            _LOGGER.error(e.__str__())

            return self.on_io_complete(e)
        

class NeoSmartBlind:
    def __init__(self, host, the_id, device, port, protocol, rail, motor_code, parent_code, http_session_factory):
        self._rail = rail
        self._parent_code = parent_code

        if self._rail < 1 or self._rail > 2:
            _LOGGER.error("{}, unknown rail: {}, please use: 1 or 2".format(device, rail))

        """Command handler to send based on correct protocol"""
        self._command_sender = None

        if protocol.lower() == "http":
            self._command_sender = NeoHttpCommandSender(http_session_factory, host, the_id, device, port, motor_code)

        elif protocol.lower() == "tcp":
            self._command_sender = NeoTcpCommandSender(host, the_id, device, port, motor_code)

        else:
            _LOGGER.error("{}, unknown protocol: {}, please use: http or tcp".format(device, protocol))
            return

        if parent_code is not None and len(parent_code) > 0:
            self._command_sender.register_parents(parent_code)

    def unique_id(self, prefix):
        return "{}.{}.{}.{}".format(prefix, self._command_sender.device, self._command_sender.motor_code, self._rail)

    async def async_set_position_by_percent(self, pos):
        """NeoBlinds works off of percent closed, but HA works off of percent open, so need to invert the percentage"""
        closed_pos = 100 - pos
        padded_position = f'{closed_pos:02}'
        return await self._command_sender.async_send_command(padded_position, self._parent_code)

    async def async_stop_command(self):
        return await self._command_sender.async_send_command(CMD_STOP, self._parent_code)

    async def async_open_cover_tilt(self, **kwargs):
        if self._rail == 1:
            return await self._command_sender.async_send_command(CMD_MICRO_UP)
        elif self._rail == 2:
            return await self._command_sender.async_send_command(CMD_MICRO_UP2)
        """Open the cover tilt."""
        return False
        
    async def async_close_cover_tilt(self, **kwargs):
        if self._rail == 1:
            return await self._command_sender.async_send_command(CMD_MICRO_DOWN)
        elif self._rail == 2:
            return await self._command_sender.async_send_command(CMD_MICRO_DOWN2)
        """Close the cover tilt."""
        return False

    """Send down command with rail support"""
    async def async_down_command(self):
        if self._rail == 1:
            return await self._command_sender.async_send_command(CMD_DOWN, self._parent_code)
        elif self._rail == 2:
            return await self._command_sender.async_send_command(CMD_DOWN2, self._parent_code)
        return False

    """Send up command with rail support"""
    async def async_up_command(self):
        if self._rail == 1:
            return await self._command_sender.async_send_command(CMD_UP, self._parent_code)
        elif self._rail == 2:
            return await self._command_sender.async_send_command(CMD_UP2, self._parent_code)
        return False

    async def async_set_fav_position(self, pos):
        if pos <= 25:
            if await self._command_sender.async_send_command(CMD_FAV):
                return 25
        if pos >= 75:
            if await self._command_sender.async_send_command(CMD_FAV_2):
                return 75
        return False




# 2021-05-13 10:20:38 INFO (SyncWorker_4) [root] Sent: http://<ip>:8838/neo/v1/transmit?id=440036000447393032323330&command=146.215-08-up&hash=.812864
# 2021-05-13 10:20:38 INFO (SyncWorker_4) [root] Neo Hub Responded with - 
