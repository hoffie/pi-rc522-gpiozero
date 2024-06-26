import logging
import threading
import time
import spidev
import gpiozero

logger = logging.getLogger(__name__)

RASPBERRY = object()
BEAGLEBONE = object()
board = RASPBERRY
PIN_MODES_BOARD = ['BOARD', 'BOARD_DEFAULT']
PIN_MODES_BCM = ['BCM']
try:
    import RPi.GPIO as GPIO  # only for pin mode selection compatibility
    PIN_MODES_BOARD.append(GPIO.BOARD)
    PIN_MODES_BCM.append(GPIO.BCM)
except ImportError:
    pass

SPIClass = spidev.SpiDev
def_pin_rst = 22
def_pin_irq = 18
def_pin_mode = 'BOARD_DEFAULT'


class RFID(object):
    pin_rst = 22
    pin_ce = 0
    pin_irq = 18

    addr_CommandReg = 0x01
    addr_ComIEnReg = 0x02
    addr_DivIEnReg = 0x03
    addr_ComIrqReq = 0x04
    addr_DivIrqReg = 0x05
    addr_ErrorReg = 0x06
    addr_Status2Reg = 0x08
    addr_FIFODataReg = 0x09
    addr_FIFOLevelReg = 0x0A
    addr_ControlReg = 0x0C
    addr_BitFramingReg = 0x0D
    addr_ModeReg = 0x11
    addr_TxControlReg = 0x14
    addr_TxASKReg = 0x15
    addr_RFCfgReg = 0x26
    addr_TModeReg = 0x2A
    addr_TPrescalerReg = 0x2B
    addr_TReloadReg2C = 0x2C
    addr_TReloadReg2D = 0x2D
    addr_CRCResultReg21 = 0x21
    addr_CRCResultReg22 = 0x22

    bit_Tx1RFEn = 1 << 0
    bit_Tx2RFEn = 1 << 1

    mode_idle = 0x00
    mode_auth = 0x0E
    mode_receive = 0x08
    mode_transmit = 0x04
    mode_transrec = 0x0C
    mode_reset = 0x0F
    mode_crc = 0x03

    auth_a = 0x60
    auth_b = 0x61

    act_read = 0x30
    act_write = 0xA0
    act_increment = 0xC1
    act_decrement = 0xC0
    act_restore = 0xC2
    act_transfer = 0xB0

    act_reqidl = 0x26
    act_reqall = 0x52
    act_anticl = 0x93
    act_anticl2 = 0x95
    act_anticl3 = 0x97
    act_select = 0x93
    act_end = 0x50

    length = 16

    antenna_gain = 0x04

    # antenna_gain
    #  defines the receiver's signal voltage gain factor:
    #  000 18 dB HEX = 0x00
    #  001 23 dB HEX = 0x01
    #  010 18 dB HEX = 0x02
    #  011 23 dB HEX = 0x03
    #  100 33 dB HEX = 0x04
    #  101 38 dB HEX = 0x05
    #  110 43 dB HEX = 0x06
    #  111 48 dB HEX = 0x07
    # 3 to 0 reserved - reserved for future use

    authed = False
    irq = threading.Event()

    def __init__(self, bus=0, device=0, speed=1000000, pin_rst=None,
                 pin_ce=0, pin_irq=None, pin_mode=def_pin_mode,
                 antenna_gain=None):
        if not pin_rst:
            # As this code may now run on non-Raspberry devices, ask for
            # explicit PIN definitions to avoid hardware damage.
            raise RuntimeError('no RST GPIO defined, please pass pin_rst= '
                               '(previous default: {def_pin_rst})')
        if not pin_irq:
            logger.info('No IRQ GPIO defined (previous default: '
                        '{def_pin_irq}), wait_for_tag() not supported')

        self.pin_rst = pin_rst
        self.pin_ce = pin_ce
        self.pin_irq = pin_irq

        self.spi = SPIClass()
        self.spi.open(bus, device)
        if board == RASPBERRY:
            self.spi.max_speed_hz = speed
        else:
            self.spi.mode = 0
            self.spi.msh = speed

        if pin_mode is not None:
            if pin_mode in PIN_MODES_BOARD:
                self.pin = lambda p: f'BOARD{p}'
            elif pin_mode in PIN_MODES_BCM:
                self.pin = lambda p: f'BCM{p}'
            else:
                raise RuntimeError("unsupported pin mode")
        if pin_rst != 0:
            self.output_rst = gpiozero.OutputDevice(self.pin(pin_rst))
            self.output_rst.on()

        # Ignore IRQ if we did not wire this
        if self.pin_irq is not None:
            self.input_irq = gpiozero.DigitalInputDevice(self.pin(pin_irq), pull_up=True)
            self.input_irq.when_deactivated = self.irq_callback

        # Change the antenna gain
        if antenna_gain is not None:
            self.antenna_gain = antenna_gain

        if pin_ce != 0:
            self.output_ce = gpiozero.OutputDevice(self.pin(pin_ce))
            self.output_ce.on()
        self.init()

    def disable_interrupts(self):
        self.dev_write(self.addr_ComIrqReq, 0x14)
        self.dev_write(self.addr_ComIEnReg, 0x80)
        self.dev_write(self.addr_DivIEnReg, 0x00)
        self.dev_write(self.addr_DivIrqReg, 0x00)

    def init(self):
        self.reset()
        self.disable_interrupts()
        self.dev_write(self.addr_TModeReg, 0x8D)
        self.dev_write(self.addr_TPrescalerReg, 0x3E)
        self.dev_write(self.addr_TReloadReg2D, 30)
        self.dev_write(self.addr_TReloadReg2C, 0)
        self.dev_write(self.addr_TxASKReg, 0x40)
        self.dev_write(self.addr_ModeReg, 0x3D)
        self.set_antenna_gain(self.antenna_gain)
        self.set_antenna(True)

    def spi_transfer(self, data):
        if self.pin_ce != 0:
            self.output_ce.off()
        r = self.spi.xfer2(data)
        if self.pin_ce != 0:
            self.output_ce.on()
        return r

    def dev_write(self, address, value):
        self.spi_transfer([(address << 1) & 0x7E, value])

    def dev_read(self, address):
        return self.spi_transfer([((address << 1) & 0x7E) | 0x80, 0])[1]

    def set_bitmask(self, address, mask):
        current = self.dev_read(address)
        self.dev_write(address, current | mask)

    def clear_bitmask(self, address, mask):
        current = self.dev_read(address)
        self.dev_write(address, current & (~mask))

    def set_antenna(self, state):
        val = self.bit_Tx1RFEn | self.bit_Tx2RFEn
        if state == True:
            current = self.dev_read(self.addr_TxControlReg)
            if ~(current & val):
                self.set_bitmask(self.addr_TxControlReg, val)
        else:
            self.clear_bitmask(self.addr_TxControlReg, val)

    def set_antenna_gain(self, gain):
        """
        Sets antenna gain from a value from 0 to 7.
        """
        if 0 <= gain <= 7:
            self.antenna_gain = gain
            self.dev_write(self.addr_RFCfgReg, (self.antenna_gain<<4))
        else:
            raise ValueError('Antenna gain has to be in the range 0...7')

    def card_write(self, command, data):
        back_data = []
        back_length = 0
        error = False
        irq = 0x00
        irq_wait = 0x00
        last_bits = None
        n = 0

        if command == self.mode_auth:
            irq = 0x12
            irq_wait = 0x10
        if command == self.mode_transrec:
            irq = 0x77
            irq_wait = 0x30

        self.dev_write(self.addr_ComIEnReg, irq | 0x80)
        self.clear_bitmask(self.addr_ComIrqReq, 0x80)
        self.set_bitmask(self.addr_FIFOLevelReg, 0x80)
        self.dev_write(self.addr_CommandReg, self.mode_idle)

        for i in range(len(data)):
            self.dev_write(self.addr_FIFODataReg, data[i])

        self.dev_write(self.addr_CommandReg, command)

        if command == self.mode_transrec:
            self.set_bitmask(self.addr_BitFramingReg, 0x80)

        i = 2000
        while True:
            n = self.dev_read(self.addr_ComIrqReq)
            i -= 1
            if ~((i != 0) and ~(n & 0x01) and ~(n & irq_wait)):
                break

        self.clear_bitmask(self.addr_BitFramingReg, 0x80)

        if i != 0:
            if (self.dev_read(self.addr_ErrorReg) & 0x1B) == 0x00:
                error = False

                if n & irq & 0x01:
                    logger.warning("Error E1")
                    error = True

                if command == self.mode_transrec:
                    n = self.dev_read(self.addr_FIFOLevelReg)
                    last_bits = self.dev_read(self.addr_ControlReg) & 0x07
                    if last_bits != 0:
                        back_length = (n - 1) * 8 + last_bits
                    else:
                        back_length = n * 8

                    if n == 0:
                        n = 1

                    if n > self.length:
                        n = self.length

                    for i in range(n):
                        back_data.append(self.dev_read(self.addr_FIFODataReg))
            else:
                logger.warning("Error E2")
                error = True

        return (error, back_data, back_length)

    def read_id(self, as_number = False):
        """
        Obtains the id (4 or 7 bytes) of a tag (if present)
        Return None on error or not present, otherwise returns tag ID

        The as_number argument can be used to return the UID as an integer. It
        defaults to a list like the rest of the API.
        """

        # Check if there is anything there
        error, tag_type = self.request()
        if error:
            return None

        # Get the UID
        error, uid = self.anticoll()
        if error:
            return None

        # Do we have an incomplete UID?!
        if uid[0] != 0x88:
            return int.from_bytes(uid[0:4], 'big') if as_number else uid[0:4]

        # Activate the tag with the incomplete UID
        error = self.select_tag(uid)
        if error:
            return None

        # Get the remaining bytes
        error, uid2 = self.anticoll2()
        if error:
            return None

        self.disable_interrupts()

        # Build the final UID without checksums
        real_uid = uid[1:-1] + uid2[:-1]
        return int.from_bytes(real_uid, 'big') if as_number else real_uid

    def request(self, req_mode=0x26):
        """
        Requests for tag.
        Returns (False, None) if no tag is present, otherwise returns (True, tag type)
        """
        error = True
        back_bits = 0

        self.dev_write(self.addr_BitFramingReg, 0x07)
        (error, back_data, back_bits) = self.card_write(self.mode_transrec, [req_mode, ])

        if error or (back_bits != 0x10):
            return (True, None)

        return (False, back_bits)

    def anticoll(self):
        """
        Anti-collision detection.
        Returns tuple of (error state, tag ID).
        """
        back_data = []
        serial_number = []

        serial_number_check = 0

        self.dev_write(self.addr_BitFramingReg, 0x00)
        serial_number.append(self.act_anticl)
        serial_number.append(0x20)

        (error, back_data, back_bits) = self.card_write(self.mode_transrec, serial_number)
        if not error:
            if len(back_data) == 5:
                for i in range(4):
                    serial_number_check = serial_number_check ^ back_data[i]

                if serial_number_check != back_data[4]:
                    error = True
            else:
                error = True

        return (error, back_data)

    def anticoll2(self):
        """
        Anti-collision detection.
        Returns tuple of (error state, tag ID).
        """
        back_data = []
        serial_number = []

        serial_number_check = 0

        self.dev_write(self.addr_BitFramingReg, 0x00)
        serial_number.append(self.act_anticl2)
        serial_number.append(0x20)

        (error, back_data, back_bits) = self.card_write(self.mode_transrec, serial_number)
        if not error:
            if len(back_data) == 5:
                for i in range(4):
                    serial_number_check = serial_number_check ^ back_data[i]

                if serial_number_check != back_data[4]:
                    error = True
            else:
                error = True

        return (error, back_data)

    def calculate_crc(self, data):
        self.clear_bitmask(self.addr_DivIrqReg, 0x04)
        self.set_bitmask(self.addr_FIFOLevelReg, 0x80)

        for i in range(len(data)):
            self.dev_write(self.addr_FIFODataReg, data[i])
        self.dev_write(self.addr_CommandReg, self.mode_crc)

        i = 255
        while True:
            n = self.dev_read(self.addr_DivIrqReg)
            i -= 1
            if not ((i != 0) and not (n & 0x04)):
                break

        ret_data = []
        ret_data.append(self.dev_read(self.addr_CRCResultReg22))
        ret_data.append(self.dev_read(self.addr_CRCResultReg21))

        return ret_data

    def select_tag(self, uid):
        """
        Selects tag for further usage.
        uid -- list or tuple with four bytes tag ID
        Returns error state.
        """
        back_data = []
        buf = []

        buf.append(self.act_select)
        buf.append(0x70)

        for i in range(5):
            buf.append(uid[i])

        crc = self.calculate_crc(buf)
        buf.append(crc[0])
        buf.append(crc[1])

        (error, back_data, back_length) = self.card_write(self.mode_transrec, buf)

        if (not error) and (back_length == 0x18):
            return False
        else:
            return True

    def card_auth(self, auth_mode, block_address, key, uid):
        """
        Authenticates to use specified block address. Tag must be selected using select_tag(uid) before auth.
        auth_mode -- RFID.auth_a or RFID.auth_b
        key -- list or tuple with six bytes key
        uid -- list or tuple with four bytes tag ID
        Returns error state.
        """
        buf = []
        buf.append(auth_mode)
        buf.append(block_address)

        for i in range(len(key)):
            buf.append(key[i])

        for i in range(4):
            buf.append(uid[i])

        (error, back_data, back_length) = self.card_write(self.mode_auth, buf)
        if not (self.dev_read(self.addr_Status2Reg) & 0x08) != 0:
            error = True

        if not error:
            self.authed = True

        return error

    def stop_crypto(self):
        """Ends operations with Crypto1 usage."""
        self.clear_bitmask(self.addr_Status2Reg, 0x08)
        self.authed = False

    def halt(self):
        """Switch state to HALT"""

        buf = []
        buf.append(self.act_end)
        buf.append(0)

        crc = self.calculate_crc(buf)
        self.clear_bitmask(self.addr_Status2Reg, 0x80)
        self.card_write(self.mode_transrec, buf)
        self.clear_bitmask(self.addr_Status2Reg, 0x08)
        self.authed = False

    def read(self, block_address):
        """
        Reads data from block. You should be authenticated before calling read.
        Returns tuple of (error state, read data).
        """
        buf = []
        buf.append(self.act_read)
        buf.append(block_address)
        crc = self.calculate_crc(buf)
        buf.append(crc[0])
        buf.append(crc[1])
        (error, back_data, back_length) = self.card_write(self.mode_transrec, buf)

        if len(back_data) != 16:
            error = True

        return (error, back_data)

    def write(self, block_address, data):
        """
        Writes data to block. You should be authenticated before calling write.
        Returns error state.
        """
        buf = []
        buf.append(self.act_write)
        buf.append(block_address)
        crc = self.calculate_crc(buf)
        buf.append(crc[0])
        buf.append(crc[1])
        (error, back_data, back_length) = self.card_write(self.mode_transrec, buf)
        if not(back_length == 4) or not((back_data[0] & 0x0F) == 0x0A):
            error = True

        if not error:
            buf_w = []
            for i in range(16):
                buf_w.append(data[i])

            crc = self.calculate_crc(buf_w)
            buf_w.append(crc[0])
            buf_w.append(crc[1])
            (error, back_data, back_length) = self.card_write(self.mode_transrec, buf_w)
            if not(back_length == 4) or not((back_data[0] & 0x0F) == 0x0A):
                error = True

        return error

    def irq_callback(self):
        logger.debug("irq_callback")
        self.irq.set()

    def wait_for_tag(self, timeout=0):
        if self.pin_irq is None:
            raise NotImplementedError('Waiting not implemented if IRQ is not used')
        logger.debug(f'wait_for_tag(timeout={timeout})')
        # enable IRQ on detect
        self.init()
        self.irq.clear()
        self.dev_write(self.addr_ComIrqReq, 0x00)
        self.dev_write(self.addr_ComIEnReg, 0xA0)
        # wait for it
        start_time = time.time()
        waiting = True
        while waiting and (timeout == 0 or ((time.time() - start_time) < timeout)):
            self.init()
            self.dev_write(self.addr_ComIrqReq, 0x00)
            self.dev_write(self.addr_ComIEnReg, 0xA0)

            # Even when using the interrupt line this is needed
            # to force the controller to re-scan regularly:
            self.dev_write(self.addr_FIFODataReg, 0x26)
            self.dev_write(self.addr_CommandReg, 0x0C)
            self.dev_write(self.addr_BitFramingReg, 0x87)
            waiting = not self.irq.wait(0.1)
        self.irq.clear()
        self.init()

    def reset(self):
        authed = False
        self.dev_write(self.addr_CommandReg, self.mode_reset)

    def cleanup(self):
        """
        Calls stop_crypto() if needed
        """
        if self.authed:
            self.stop_crypto()

    def util(self):
        """
        Creates and returns RFIDUtil object for this RFID instance.
        If module is not present, returns None.
        """
        try:
            from .util import RFIDUtil
            return RFIDUtil(self)
        except ImportError:
            return None
