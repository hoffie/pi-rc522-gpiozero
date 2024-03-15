#!/usr/bin/env python
import logging
import pirc522

PIN_IRQ = None  # e.g. 18
PIN_RST = PLEASE_DEFINE_ME  # e.g. 22
logger = logging.getLogger('ReadUid')


if __name__ == '__main__':
    logging.basicConfig(level=logging.DEBUG,
                        format='%(asctime)s  %(levelname).5s  %(message)s')

    try:
        reader = pirc522.RFID(pin_mode='BOARD', pin_rst=PIN_RST, pin_irq=PIN_IRQ, antenna_gain=3)
        while True:
            reader.wait_for_tag()
            uid = reader.read_id(True)
            if uid is not None:
                logger.info(f'UID: {uid:X}')

    except KeyboardInterrupt:
        pass

    finally:
        reader.cleanup()
