"""@package DW1000
This python module contains low-level functions to interact with the DW1000 chip using a Raspberry Pi 3.
"""

import time
import math
from random import randrange
import spidev
import RPi.GPIO as GPIO
import logging
import copy

import DW1000Constants as C
from DW1000Register import DW1000Register
import MAC
from Helper import convertStringToByte, writeValueToBytes

GPIO.setwarnings(False)

class DW1000:
    """
    DW1000 management class.

    Provides all functionality to a client to send/receive messages and perform ranging.

    Args:
        cs: Chip select pin number
        rst: Reset pin number
        irq: Interrupt pin number

    Attributes:
        cs: Chip select pin
        rst: Reset pin
        irq: Interrupt pin
        spi: SPI device
        dblbuffon (bool): Double buffer state (enabled/disabled)
        sysctrl (DW1000Register): DW1000 system control register
        chanctrl (DW1000Register): DW1000 channel control register
        syscfg (DW1000Register): DW1000 system config register
        sysmask (DW1000Register): DW1000 system mask register (mask interrupts)
        txfctrl (DW1000Register): DW1000 transmission framce control register
        sysstatus (DW1000Register): DW1000 system status register
        gpiomode (DW1000Register): DW1000 gpio mode register, used to enable leds
        pmscctrl0 (DW1000Register): DW1000 pmscctrl0 register
        pmscledc (DW1000Register): DW1000 led control register, used to control leds
        otpctrl (DW1000Register): DW1000 otpctrl register
        panadr (DW1000Register): DW1000 private area network address register
        eui (DW1000Register): DW1000 extended unique identifer register
        ackrespt (DW1000Register): DW1000 ackrept register
        rxfinfo (DW1000Register): DW1000 received frame information
        seqNum: Track sequence numbers of send frames (increase after send)
        operationMode: Mode of operation
        permanentReceive (bool): Enable/disable permanent receiver
        extendedAddress: Long form address of DW1000
        shortAddress: Short form address, extracted last 2 bytes from extendedAddress
        interruptCallback: Function to call on interrupt receiption
    """
    def __init__(self, cs, rst, irq):
        self.cs = cs #: Test
        self.rst = rst # Test2
        self.irq = irq

        self.spi = None

        self.dblbuffon = False

        self.sysctrl = DW1000Register(C.SYS_CTRL, C.NO_SUB, 4)
        self.chanctrl = DW1000Register(C.CHAN_CTRL, C.NO_SUB, 4)
        self.syscfg = DW1000Register(C.SYS_CFG, C.NO_SUB, 4)
        self.sysmask = DW1000Register(C.SYS_MASK, C.NO_SUB, 4)
        self.txfctrl = DW1000Register(C.TX_FCTRL, C.NO_SUB, 5)
        self.sysstatus = DW1000Register(C.SYS_STATUS, C.NO_SUB, 5)
        self.gpiomode = DW1000Register(C.GPIO_CTRL, C.GPIO_MODE_SUB, 4)
        self.pmscctrl0 = DW1000Register(C.PMSC, C.PMSC_CTRL0_SUB, 4)
        self.pmscledc = DW1000Register(C.PMSC, C.PMSC_LEDC_SUB, 4)
        self.otpctrl = DW1000Register(C.OTP_IF, C.OTP_CTRL_SUB, 2)
        self.panadr = DW1000Register(C.PANADR, C.NO_SUB, 4)
        self.eui = DW1000Register(C.EUI, C.NO_SUB, 8)
        self.ackrespt = DW1000Register(C.ACK_RESP_T, C.NO_SUB, 4)
        self.rxfinfo = DW1000Register(C.RX_FINFO, C.NO_SUB, 4)

        self.seqNum = randrange(0, 256) # Sequence number for transmitted frames | hashmap and per connection number?

        self.operationMode = [None] * 6 # [dataRate, pulseFrequency, pacSize, preambleLength, channel, preacode]
        self.permanentReceive = False

        self.extendedAddress = None
        self.shortAddress = None

        # Main interrupt callback
        self.interruptCallback = lambda: None


    def begin(self):
        """
        Perform setup of DW1000.

        Initialize GPIO on Host, establish SPI connection and initialize the DWM1000.
        """
        GPIO.setmode(GPIO.BCM)

        # Reset to ensure correct operation of module
        self.hardReset()
        time.sleep(C.INIT_DELAY)

        # Setup SPI
        try:
            self.spi = spidev.SpiDev()
            self.spi.open(0, 0)
            self.spi.no_cs = True
            self.spi.max_speed_hz = 4000000
        except Exception as e:
            logging.error(str(e))
            raise

        # Setup Host GPIO
        GPIO.setup(self.cs, GPIO.OUT, initial=GPIO.HIGH)
        GPIO.setup(self.irq, GPIO.IN, pull_up_down=GPIO.PUD_DOWN) # TODO: CHECK
        #self.enableInterrupt()

        self.enableClock(C.AUTO_CLOCK)

        self.softReset()

        # Default system configuration
        self.syscfg.clear()
        self.syscfg.setBits((C.HIRQ_POL_BIT, C.DIS_DRXB_BIT), True)
        self.writeRegister(self.syscfg)

        # Default DW1000 GPIO configuration, enable LEDS
        self.enableLeds()

        # clear interrupts configuration
        self.sysmask.clear()
        self.writeRegister(self.sysmask)

        self.enableClock(C.XTI_CLOCK)
        self.manageLDE()
        self.enableClock(C.AUTO_CLOCK)

        self.spi.max_speed_hz = 7800000

        logging.info("Started DW1000")


    def stop(self):
        """
        Break down connection to DWM1000

        Release resources (GPIO, SPI).
        """
        self.disableInterrupt()
        GPIO.setup(self.rst, GPIO.OUT, initial=GPIO.LOW)
        time.sleep(0.1)
        self.spi.close()
        GPIO.cleanup()
        logging.info("Stopped DW1000")


    def hardReset(self):
        """
        Reset the DWM1000 by driving the reset line low for a short time.
        """
        # Low for 100ms for reset
        GPIO.setup(self.rst, GPIO.OUT, initial=GPIO.LOW)
        time.sleep(0.20)
        # Reset pin to high impedance open drain
        GPIO.cleanup(self.rst)


    def softReset(self):
        """
        This function performs a soft reset on the DW1000 chip.
        """
        self.readRegister(self.pmscctrl0)
        self.pmscctrl0[0] = C.SOFT_RESET_SYSCLKS
        self.writeRegister(self.pmscctrl0)
        self.pmscctrl0[3] = C.SOFT_RESET_CLEAR
        self.writeRegister(self.pmscctrl0)
        self.pmscctrl0[0] = C.SOFT_RESET_CLEAR
        self.pmscctrl0[3] = C.SOFT_RESET_SET
        self.writeRegister(self.pmscctrl0)
        self.idle()


    def enableLeds(self):
        """
        Enable all 4 led outputs.
        """
        self.gpiomode.clear()
        self.gpiomode.setBits((6, 8, 10, 12), True)
        self.writeRegister(self.gpiomode)
        self.readRegister(self.pmscctrl0)
        self.pmscctrl0[2] |= 0b10000100
        self.writeRegister(self.pmscctrl0)
        self.pmscledc.clear()
        self.pmscledc[0] = C.PMSC_LEDC_BLINK_TIM_BYTE
        self.pmscledc.setBit(C.PMSC_LEDC_BLINKEN_BIT, True)
        self.writeRegister(self.pmscledc)


    def readRegister(self, reg):
        """
        Get register content from DWM1000.

        This function takes a register control structure and reads the contents on the DWM1000
        into the host local buffer.

        Args:
            reg (DW1000Register): Register to read
        """
        self.readBytes(reg.address, reg.subaddress, reg.data, reg.size)


    def writeRegister(self, reg):
        """
        Set register content on DWM1000.

        This function takes a register control structure and writes the contents to the DWM1000.

        Args:
            reg (DW1000Register): Register to write
        """
        self.writeBytes(reg.address, reg.subaddress, reg.data, reg.size)


    def toggleHSRBP(self):
        """
        Toggle host side receive buffer pointer.
        """
        if self.dblbuffon:
            # Save interrupt mask and change HRBPT bits
            oldmask = self.sysmask.data.copy()
            self.sysmask.setBits((C.MRXFCE_BIT, C.MRXFCG_BIT, C.MRXDFR_BIT, C.MLDEDONE_BIT), False)
            self.writeRegister(self.sysmask)

            # Toggle HSRBP
            self.sysctrl.setBit(C.HRBPT_BIT, True)
            self.writeRegister(self.sysctrl)

            # Restore interrupt mask
            self.sysmask.data = oldmask
            self.writeRegister(self.sysmask)


    def syncHSRBP(self):
        """
        Synchronize host side receive buffer pointer to ic side receive buffer pointer.
        """
        self.readRegister(self.sysstatus)
        hsrbp = self.sysstatus.getBit(C.HSRBP_BIT)
        icrbp = self.sysstatus.getBit(C.ICRBP_BIT)
        if hsrbp == icrbp:
            self.toggleHSRBP()


    def forceTRxOff(self):
        """
        Force shutdown transmitte/receiver.
        """
        self.readRegister(self.sysmask)
        mask = copy.copy(self.sysmask.data)

        self.disableInterrupt()

        self.sysmask.setAll(0x00)
        self.writeRegister(self.sysmask)

        self.sysctrl.clear()
        self.sysctrl.setBit(C.TRXOFF_BIT, True)
        self.writeRegister(self.sysctrl)

        self.clearStatus(C.SYS_STATUS_ALL_TX + C.SYS_STATUS_ALL_RX_ERR + \
                            C.SYS_STATUS_ALL_RX_TO + C.SYS_STATUS_ALL_RX_GOOD)

        self.syncHSRBP()

        self.sysmask.data = mask
        self.writeRegister(self.sysmask)

        #self.enableInterrupt()
        self.sysctrl.setBit(C.WAIT4RESP_BIT, False)


    def disableInterrupt(self):
        GPIO.remove_event_detect(self.irq)


    def enableInterrupt(self):
        try:
            GPIO.add_event_detect(self.irq, GPIO.RISING, callback=self.handleInterrupt)
        except:
            logging.error("Failed to enable interrupt!")

    def enableDoubleBuffer(self):
        self.dblbuffon = True
        self.syncHSRBP()
        self.syscfg.setBit(C.HSRBP_BIT, True)
        self.writeRegister(self.syscfg)


    def disableDoubleBuffer(self):
        self.dblbuffon = False
        self.syncHSRBP()
        self.syscfg.setBit(C.HSRBP_BIT, False)
        self.writeRegister(self.syscfg)


    def rxreset(self):
        self.writeBytes(C.PMSC, C.PMSC_CTRL0_SUB+0x3, bytearray([C.SOFT_RESET_RX]), 1)
        self.writeBytes(C.PMSC, C.PMSC_CTRL0_SUB+0x3, bytearray([C.SOFT_RESET_SET]), 1)


    def readBytes(self, cmd, offset, data, n):
        """
        This function reads n bytes from the specified register to the array data.

        Args:
            cmd: Address of register to read
            offset: Offset inside the register
            data: Array for the data to be stored in
            n: Number of bytes to read
        """
        header = bytearray(3)
        headerLen = 1

        if offset == C.NO_SUB:
            header[0] = C.READ | cmd
        else:
            header[0] = C.READ_SUB | cmd
            if offset < 128:
                header[1] = offset
                headerLen = headerLen + 1
            else:
                header[1] = (C.RW_SUB_EXT | offset) & C.MASK_LS_BYTE
                header[2] = offset >> 7
                headerLen = headerLen + 2

        GPIO.output(self.cs, GPIO.LOW)

        #_data = self.spi.xfer2(header[0:headerLen] + bytearray(n))

        for i in range(0, headerLen):
            self.spi.xfer([int(header[i])])

        for i in range(0, n):
            data[i] = self.spi.xfer([C.JUNK])[0]

        GPIO.output(self.cs, GPIO.HIGH)

        #for i in range(0, n):
        #    data[i] = _data[headerLen + i]


    def writeBytes(self, cmd, offset, data, dataSize):
        """
        This function writes dataSize bytes from the specified array to the register.

        Args:
            cmd: Address of register to write
            offset: Offset inside the register
            data: Array for the data to be written
            dataSize: Number of bytes to write
        """
        header = bytearray(3)
        headerLen = 1

        if offset == C.NO_SUB:
            header[0] = C.WRITE | cmd
        else:
            header[0] = C.WRITE_SUB | cmd
            if offset < 128:
                header[1] = offset
                headerLen = headerLen + 1
            else:
                header[1] = (C.RW_SUB_EXT | offset) & C.MASK_LS_BYTE
                header[2] = offset >> 7
                headerLen = headerLen + 2

        GPIO.output(self.cs, GPIO.LOW)

        for i in range(0, headerLen):
            self.spi.xfer([int(header[i])])

        for i in range(0, dataSize):
            if (data[i] != None):
                self.spi.xfer([int(data[i])])

        #self.spi.xfer2(header[0:headerLen] + data[0:dataSize])

        GPIO.output(self.cs, GPIO.HIGH)


    def enableClock(self, clock):
        """
        This function manages the dw1000 chip's clock by setting up the proper registers to activate the specified clock mode chosen.

        Args:
            clock: An hex value corresponding to the clock mode wanted:
                AUTO=0x00
                XTI=0x01
                PLL=0X02.
        """
        self.readRegister(self.pmscctrl0)
        if clock == C.AUTO_CLOCK:
            self.pmscctrl0[0] = C.AUTO_CLOCK
            self.pmscctrl0[1] = self.pmscctrl0[1] & C.ENABLE_CLOCK_MASK1
        elif clock == C.XTI_CLOCK:
            self.pmscctrl0[0] = self.pmscctrl0[0] & C.ENABLE_CLOCK_MASK2
            self.pmscctrl0[0] = self.pmscctrl0[0] | 1
        self.writeRegister(self.pmscctrl0)


    def getStatusRegisterString(self):
        """
        For debugging purposes.

        Create and return a string with all the information inside the status register.

        Returns:
            (string): Human readable string of status register contents
        """
        statusstr = ""

        statusstr += "System status bits:\n"
        statusstr += "IRQS:\t\t" + str(self.sysstatus.getBit(C.IRQS_BIT)) + "\n"
        statusstr += "CPLOCK:\t\t" + str(self.sysstatus.getBit(C.CPLOCK_BIT)) + "\n"
        statusstr += "ESYNCR:\t\t" + str(self.sysstatus.getBit(C.ESYNCR_BIT)) + "\n"
        statusstr += "AAT:\t\t" + str(self.sysstatus.getBit(C.AAT_BIT)) + "\n"
        statusstr += "TXFRB:\t\t" + str(self.sysstatus.getBit(C.TXFRB_BIT)) + "\n"
        statusstr += "TXPRS:\t\t" + str(self.sysstatus.getBit(C.TXPRS_BIT)) + "\n"
        statusstr += "TXPHS:\t\t" + str(self.sysstatus.getBit(C.TXPHS_BIT)) + "\n"
        statusstr += "TXFRS:\t\t" + str(self.sysstatus.getBit(C.TXFRS_BIT)) + "\n"
        statusstr += "RXPRD:\t\t" + str(self.sysstatus.getBit(C.RXPRD_BIT)) + "\n"
        statusstr += "RXSFDD:\t\t" + str(self.sysstatus.getBit(C.RXSFDD_BIT)) + "\n"
        statusstr += "LDEDONE:\t" + str(self.sysstatus.getBit(C.LDEDONE_BIT)) + "\n"
        statusstr += "RXPHD:\t\t" + str(self.sysstatus.getBit(C.RXPHD_BIT)) + "\n"
        statusstr += "RXPHE:\t\t" + str(self.sysstatus.getBit(C.RXPHE_BIT)) + "\n"
        statusstr += "RXDFR:\t\t" + str(self.sysstatus.getBit(C.RXDFR_BIT)) + "\n"
        statusstr += "RXFCG:\t\t" + str(self.sysstatus.getBit(C.RXFCG_BIT)) + "\n"
        statusstr += "RXFCE:\t\t" + str(self.sysstatus.getBit(C.RXFCE_BIT)) + "\n"
        statusstr += "RXRFSL:\t\t" + str(self.sysstatus.getBit(C.RXRFSL_BIT)) + "\n"
        statusstr += "RXRFTO:\t\t" + str(self.sysstatus.getBit(C.RXRFTO_BIT)) + "\n"
        statusstr += "LDEERR:\t\t" + str(self.sysstatus.getBit(C.LDEERR_BIT)) + "\n"
        statusstr += "RXOVRR:\t\t" + str(self.sysstatus.getBit(C.RXOVRR_BIT)) + "\n"
        statusstr += "RXPTO:\t\t" + str(self.sysstatus.getBit(C.RXPTO_BIT)) + "\n"
        statusstr += "GPIOIRQ:\t" + str(self.sysstatus.getBit(C.GPIOIRQ_BIT)) + "\n"
        statusstr += "SLP2INIT:\t" + str(self.sysstatus.getBit(C.SLP2INIT_BIT)) + "\n"
        statusstr += "RFPLL_LL:\t" + str(self.sysstatus.getBit(C.RFPLL_LL_BIT)) + "\n"
        statusstr += "CLKPLL_LL:\t" + str(self.sysstatus.getBit(C.CLKPLL_LL_BIT)) + "\n"
        statusstr += "RXSFDTO:\t" + str(self.sysstatus.getBit(C.RXSFDTO_BIT)) + "\n"
        statusstr += "HPDWARN:\t" + str(self.sysstatus.getBit(C.HPDWARN_BIT)) + "\n"
        statusstr += "TXBERR:\t\t" + str(self.sysstatus.getBit(C.TXBERR_BIT)) + "\n"
        statusstr += "AFFREJ:\t\t" + str(self.sysstatus.getBit(C.AFFREJ_BIT)) + "\n"
        statusstr += "HSRBP:\t\t" + str(self.sysstatus.getBit(C.HSRBP_BIT)) + "\n"
        statusstr += "ICRBP:\t\t" + str(self.sysstatus.getBit(C.ICRBP_BIT)) + "\n"
        statusstr += "RXRSCS:\t\t" + str(self.sysstatus.getBit(C.RXRSCS_BIT)) + "\n"
        statusstr += "RXPREJ:\t\t" + str(self.sysstatus.getBit(C.RXPREJ_BIT)) + "\n"
        statusstr += "TXPUTE:\t\t" + str(self.sysstatus.getBit(C.TXPUTE_BIT)) + "\n"

        return statusstr


    def idle(self):
        """
        This function puts the chip into idle mode.
        """
        self.sysctrl.clear()
        self.sysctrl.setBit(C.TRXOFF_BIT, True)
        self.writeRegister(self.sysctrl)


    def handleInterrupt(self, channel):
        """
        Callback invoked on the rising edge of the interrupt pin. Handle the configured interruptions.

        Args:
            channel: Unused
        """
        logging.debug("Interrupt received")
        if callable(self.interruptCallback):
            self.interruptCallback()


    def manageLDE(self):
        """
        This function manages the LDE micro-code. It is to setup the power management and system control unit as well as the OTP memory interface.
        This is necessary as part of the DW1000 initialisation, since it is important to get timestamp and diagnostic info from received frames.
        """
        self.readRegister(self.pmscctrl0)
        self.readRegister(self.otpctrl)

        self.pmscctrl0[0] = C.LDE_L1STEP1
        self.pmscctrl0[1] = C.LDE_L1STEP2
        self.otpctrl[0] = C.LDE_L2STEP1
        self.otpctrl[1] = C.LDE_L2STEP2

        self.writeRegister(self.pmscctrl0)
        self.writeRegister(self.otpctrl)

        # wait 150 us before writing the 0x36:00 sub-register, see 2.5.5.10 of the DW1000 user manual.
        time.sleep(C.PMSC_CONFIG_DELAY)

        self.pmscctrl0[0] = C.LDE_L3STEP1
        self.pmscctrl0[1] = C.LDE_L3STEP2

        self.writeRegister(self.pmscctrl0)


    def enableMode(self, mode):
        """
        This function configures the DW1000 chip to perform with a specific mode. It sets up the TRX rate the TX pulse frequency and the preamble length.

        Args:
            mode (list): Mode from DW1000Constants
        """
        # setDataRate
        self.setDataRate(mode[C.DATA_RATE_BIT])

        # setPulseFreq
        self.setPulseFreq(mode[C.PULSE_FREQUENCY_BIT])

        # setPreambleLength
        self.setPreambleLength(mode[C.PREAMBLE_LENGTH_BIT])

        # setPACSize
        self.operationMode[C.PAC_SIZE_BIT] = mode[C.PAC_SIZE_BIT]

        # setChannel
        self.setChannel(mode[C.CHANNEL_BIT])

        # setPreambleCode
        self.setPreambleCode(mode[C.PREAMBLE_CODE_BIT])

        # setAckTim
        self.setW4RTim(0)
        if mode[C.DATA_RATE_BIT] == C.TRX_RATE_110KBPS:
            self.setAckTim(0)
        elif mode[C.DATA_RATE_BIT] == C.TRX_RATE_850KBPS:
            self.setAckTim(2)
        elif mode[C.DATA_RATE_BIT] == C.TRX_RATE_6800KBPS:
            self.setAckTim(3)


    def newConfiguration(self):
        """
        This function resets the DW1000 chip to the idle state mode and reads all the configuration registers to prepare for a new configuration.
        """
        self.idle()
        self.readRegister(self.panadr)
        self.readRegister(self.syscfg)
        self.readRegister(self.chanctrl)
        self.readRegister(self.txfctrl)
        self.readRegister(self.sysmask)


    def commitConfiguration(self):
        """
        This function commits the configuration stored in the arrays previously filled. It writes into the corresponding registers to apply the changes to the DW1000 chip.
        It also tunes the chip according to the current enabled mode.
        """
        self.writeRegister(self.panadr)
        self.writeRegister(self.syscfg)
        self.writeRegister(self.chanctrl)
        self.writeRegister(self.txfctrl)
        self.writeRegister(self.sysmask)

        self.tune()


    def setAntennaDelay(self, val):
        """
        This function sets the DW1000 chip's antenna delay value which needs to be calibrated to have better ranging accuracy.

        Args:
            val: The antenna delay value which will be configured into the chip.
        """
        antennaDelayBytes = bytearray(5)
        writeValueToBytes(antennaDelayBytes, val, 5)
        self.writeBytes(C.TX_ANTD, C.NO_SUB, antennaDelayBytes, 2)
        self.writeBytes(C.LDE_CTRL, C.LDE_RXANTD_SUB, antennaDelayBytes, 2)


    def setEUI(self, currentAddress):
        """
        This function sets the extended unique identifier of the chip according to the value specified by the user in setup.

        Args:
            currentAddress (bytes): The new EUI
        """
        for i in range(0, 8):
            self.eui[i] = currentAddress[8 - i - 1]
        self.writeRegister(self.eui)


    def setDeviceAddress(self, value):
        """
        This function sets the device's short address according to the specified value.

        Args:
            value (bytes): The address you want to set to the chip.
        """
        self.panadr[0] = value & C.MASK_LS_BYTE
        self.panadr[1] = (value >> 8) & C.MASK_LS_BYTE


    def setNetworkId(self, value):
        """
        This function sets the device's network ID according to the specified value.

        Args:
            value: The network id you want to assign to the chip.
        """
        self.panadr[2] = value & C.MASK_LS_BYTE
        self.panadr[3] = (value >> 8) & C.MASK_LS_BYTE


    def setDataRate(self, rate):
        """
        This function set the data rate.

        See DW1000Constants for valid data rates.

        Args:
            rate: Speed of the module
        """
        rate = rate & C.MASK_LS_2BITS
        self.txfctrl[1] = self.txfctrl[1] & C.ENABLE_MODE_MASK1
        self.txfctrl[1] = self.txfctrl[1] | ((rate << 5) & C.MASK_LS_BYTE)

        if rate == C.TRX_RATE_110KBPS:
            self.syscfg.setBit(C.RXM110K_BIT, True)
        else:
            self.syscfg.setBit(C.RXM110K_BIT, False)

        if rate == C.TRX_RATE_6800KBPS:
            self.chanctrl.setBits((C.DWSFD_BIT, C.TNSSFD_BIT, C.RNSSFD_BIT), False)
        else:
            self.chanctrl.setBits((C.DWSFD_BIT, C.TNSSFD_BIT, C.RNSSFD_BIT), True)

        if rate == C.TRX_RATE_850KBPS:
            sfdLength = bytearray([C.SFD_LENGTH_850KBPS])
        elif rate == C.TRX_RATE_6800KBPS:
            sfdLength = bytearray([C.SFD_LENGTH_6800KBPS])
        else:
            sfdLength = bytearray([C.SFD_LENGTH_OTHER])

        self.writeBytes(C.USR_SFD, C.SFD_LENGTH_SUB, sfdLength, 1)
        self.operationMode[C.DATA_RATE_BIT] = rate


    def setPulseFreq(self, freq):
        """
        This function sets the pulse frequency.

        Args:
            freq: Frequency identifer from DW1000Constants
        """
        freq = freq & C.MASK_LS_2BITS
        self.txfctrl[2] = self.txfctrl[2] & C.ENABLE_MODE_MASK2
        self.txfctrl[2] = self.txfctrl[2] | (freq & C.MASK_LS_BYTE)
        self.chanctrl[2] = self.chanctrl[2] & C.ENABLE_MODE_MASK3
        self.chanctrl[2] = self.chanctrl[2] | ((freq << 2) & C.MASK_LS_BYTE)
        self.operationMode[C.PULSE_FREQUENCY_BIT] = freq


    def setPreambleLength(self, prealen):
        """
        This function sets the preamble length.

        Args:
            prealen: Prealen identifer from DW1000Constants
        """
        prealen = prealen & C.MASK_NIBBLE
        self.txfctrl[2] = self.txfctrl[2] & C.ENABLE_MODE_MASK4
        self.txfctrl[2] = self.txfctrl[2] | ((prealen << 2) & C.MASK_LS_BYTE)
        self.operationMode[C.PREAMBLE_LENGTH_BIT] = prealen


    def setChannel(self, channel):
        """
        This function configures the DW1000 chip to enable a the specified channel of operation.

        Args:
            channel: The channel value you want to assign to the chip.
        """
        channel = channel & C.MASK_NIBBLE
        self.chanctrl[0] = ((channel | (channel << 4)) & C.MASK_LS_BYTE)
        self.operationMode[C.CHANNEL_BIT] = channel


    def setPreambleCode(self, preacode):
        """
        This function sets the preamble code used for the frames, depending on the the pulse repetition frequency and the channel used.

        Args:
            preacode: The preamble code type you want to assign to the chip.
        """
        preacode = preacode & C.PREACODE_MASK1
        self.chanctrl[2] = self.chanctrl[2] & C.PREACODE_MASK2
        self.chanctrl[2] = self.chanctrl[2] | ((preacode << 6) & C.MASK_LS_BYTE)
        self.chanctrl[3] = 0x00
        self.chanctrl[3] = ((((preacode >> 2) & C.PREACODE_MASK3) |
                        (preacode << 3)) & C.MASK_LS_BYTE)
        self.operationMode[C.PREAMBLE_CODE_BIT] = preacode


    def setAckTim(self, value):
        self.readRegister(self.ackrespt)
        self.ackrespt[3] = value
        self.writeRegister(self.ackrespt)


    def setW4RTim(self, value):
        self.readRegister(self.ackrespt)
        self.ackrespt[2] = 0x0F & (value >> 16)
        self.ackrespt[1] = 0xFF & (value >> 8)
        self.ackrespt[0] = 0xFF & (value)
        self.writeRegister(self.ackrespt)


    def tune(self):
        """
        This function tunes/configures dw1000 chip's registers according to the enabled mode. Although the DW1000 will power up in a usable mode for the default configuration,
        some of the register defaults are sub optimal and should be overwritten before using the chip in the default mode. See 2.5.5 of the user manual.
        """
        agctune1 = DW1000Register(C.AGC_CTRL, C.AGC_TUNE1_SUB, 2)
        agctune2 = DW1000Register(C.AGC_CTRL, C.AGC_TUNE2_SUB, 4)
        agctune3 = DW1000Register(C.AGC_CTRL, C.AGC_TUNE3_SUB, 2)
        drxtune0b = DW1000Register(C.DRX_CONF, C.DRX_TUNE0b_SUB, 2)
        drxtune1a = DW1000Register(C.DRX_CONF, C.DRX_TUNE1a_SUB, 2)
        drxtune1b = DW1000Register(C.DRX_CONF, C.DRX_TUNE1b_SUB, 2)
        drxtune2 = DW1000Register(C.DRX_CONF, C.DRX_TUNE2_SUB, 4)
        drxtune4H = DW1000Register(C.DRX_CONF, C.DRX_TUNE4H_SUB, 2)
        rfrxctrlh = DW1000Register(C.RF_CONF, C.RF_RXCTRLH_SUB, 1)
        rftxctrl = DW1000Register(C.RF_CONF, C.RF_TXCTRL_SUB, 4)
        tcpgdelay = DW1000Register(C.TX_CAL, C.TC_PGDELAY_SUB, 1)
        fspllcfg = DW1000Register(C.FS_CTRL, C.FS_PLLCFG_SUB, 4)
        fsplltune = DW1000Register(C.FS_CTRL, C.FS_PLLTUNE_SUB, 1)
        ldecfg1 = DW1000Register(C.LDE_CTRL, C.LDE_CFG1_SUB, 1)
        ldecfg2 = DW1000Register(C.LDE_CTRL, C.LDE_CFG2_SUB, 2)
        lderepc = DW1000Register(C.LDE_CTRL, C.LDE_REPC_SUB, 2)
        txpower = DW1000Register(C.TX_POWER, C.NO_SUB, 4)
        fsxtalt = DW1000Register(C.FS_CTRL, C.FS_XTALT_SUB, 1)
        preambleLength = self.operationMode[C.PREAMBLE_LENGTH_BIT]
        channel = self.operationMode[C.CHANNEL_BIT]

        self.tuneAgcTune1(agctune1)
        agctune2.writeValue(C.AGC_TUNE2_OP)
        agctune2.writeValue(C.AGC_TUNE3_OP)
        self.tuneDrxTune0b(drxtune0b)
        self.tuneDrxTune1aAndldecfg2(drxtune1a, ldecfg2)
        self.tuneDrxtune1b(drxtune1b)
        self.tuneDrxTune2(drxtune2)

        if preambleLength == C.TX_PREAMBLE_LEN_64:
            drxtune4H.writeValue(C.DRX_TUNE4H_64)
        else:
            drxtune4H.writeValue(C.DRX_TUNE4H_128)

        if channel != C.CHANNEL_4 and channel != C.CHANNEL_7:
            rfrxctrlh.writeValue(C.RF_RXCTRLH_1235)
        else:
            rfrxctrlh.writeValue(C.RF_RXCTRLH_147)

        self.tuneAccToChan(rftxctrl, tcpgdelay, fspllcfg, fsplltune, txpower)

        ldecfg1.writeValue(C.LDE_CFG1_OP)
        self.tunelderepc(lderepc)

        buf_otp = [None] * 4
        buf_otp = self.readBytesOTP(C.OTP_XTAL_ADDRESS, buf_otp)
        if buf_otp[0] == 0:
                fsxtalt.writeValue((C.TUNE_OPERATION & C.TUNE_MASK1) | C.TUNE_MASK2)
        else:
                fsxtalt.writeValue((buf_otp[0] & C.TUNE_MASK1) | C.TUNE_MASK2)

        self.writeRegister(agctune1)
        self.writeRegister(agctune2)
        self.writeRegister(agctune3)
        self.writeRegister(drxtune0b)
        self.writeRegister(drxtune1a)
        self.writeRegister(drxtune1b)
        self.writeRegister(drxtune2)
        self.writeRegister(drxtune4H)
        self.writeRegister(ldecfg1)
        self.writeRegister(ldecfg2)
        self.writeRegister(lderepc)
        self.writeRegister(txpower)
        self.writeRegister(rfrxctrlh)
        self.writeRegister(rftxctrl)
        self.writeRegister(tcpgdelay)
        self.writeRegister(fsplltune)
        self.writeRegister(fspllcfg)
        self.writeRegister(fsxtalt)


    def generalConfiguration(self, address, pan, mode):
        """
        This function configures the DW1000 chip with general settings. It also defines the address and the network ID used by the device. It finally prints the
        configured device.

        Args:
            address (bytes): The eui address you want to set the device to.
            pan (bytes): The pan address you want to set the device to.
            mode (bytes): Operation mode identifer from DW1000Constants
        """
        currentAddress = convertStringToByte(address)
        currentShortAddress = currentAddress[-2:]
        self.setEUI(currentAddress)
        deviceAddress = currentShortAddress[0] * 256 + currentShortAddress[1]

        # configure mode, network
        self.newConfiguration()
        self.setDeviceAddress(deviceAddress)
        self.setNetworkId(pan)
        self.enableMode(mode)
        self.setAntennaDelay(C.ANTENNA_DELAY)
        self.commitConfiguration()


    def getDeviceInfoString(self):
        """
        This function returns some infos about the DW1000 module

        Returns:
            String containing device information
        """
        devinfostr = ""

        devid = DW1000Register(C.DEV_ID, C.NO_SUB, 4)
        self.readRegister(devid)
        self.readRegister(self.eui)
        self.readRegister(self.panadr)
        devinfostr += "\nDevice ID {:02X} - model: {}, version: {}, revision: {}\n".format(
            (devid[3] << 8) | devid[2], (devid[1]), (devid[0] >> 4) & C.MASK_NIBBLE, devid[0] & C.MASK_NIBBLE)
        devinfostr += "Unique ID: {:02X}:{:02X}:{:02X}:{:02X}:{:02X}:{:02X}:{:02X}:{:02X}\n".format(
            *(reversed([eui for eui in self.eui])))
        devinfostr += "Network ID & Device Address: PAN: {:02X}, Short Address: {:02X}\n".format(
            ((self.panadr[3] << 8) | self.panadr[2]), ((self.panadr[1] << 8) | self.panadr[0]))
        devinfostr += self.getDeviceModeInfoString()

        return devinfostr


    """
    Tuning functions
    See tune() for more information
    """


    def tuneAgcTune1(self, agctune1):
        """
        This function fills the array for the tuning of agctune1 according to the datasheet and the enabled mode.

        Args:
            agctune1 (DW1000Register): Register which will store the correct values for agctune1.

        """
        pulseFrequency = self.operationMode[C.PULSE_FREQUENCY_BIT]
        if pulseFrequency == C.TX_PULSE_FREQ_16MHZ:
            agctune1.writeValue(C.AGC_TUNE1_16MHZ_OP)
        elif pulseFrequency == C.TX_PULSE_FREQ_64MHZ:
            agctune1.writeValue(C.AGC_TUNE1_DEFAULT_OP)


    def tuneDrxTune0b(self, drxtune0b):
        """
        This function fills the array for the tuning of drxtune0b according to the datasheet and the enabled mode.

        Args:
            drxtune0b (DW1000Register): Register which will store the correct values for drxtune0b.
        """
        dataRate = self.operationMode[C.DATA_RATE_BIT]
        if dataRate == C.TRX_RATE_110KBPS:
            drxtune0b.writeValue(C.DRX_TUNE0b_110KBPS_NOSTD_OP)
        elif dataRate == C.TRX_RATE_850KBPS:
            drxtune0b.writeValue(C.DRX_TUNE0b_850KBPS_NOSTD_OP)
        elif dataRate == C.TRX_RATE_6800KBPS:
            drxtune0b.writeValue(C.DRX_TUNE0b_6800KBPS_STD_OP)


    def tuneDrxTune1aAndldecfg2(self, drxtune1a, ldecfg2):
        """
        This function fills the array for the tuning of drxtune1a and ldecfg2 according to the datasheet and the enabled mode.

        Args:
            drxtune1a (DW1000Register): Register which will store the correct values for the drxtune1a.
            ldecfg2 (DW1000Register): Register which will store the correct values for ldecfg2.
        """
        pulseFrequency = self.operationMode[C.PULSE_FREQUENCY_BIT]
        if pulseFrequency == C.TX_PULSE_FREQ_16MHZ:
            drxtune1a.writeValue(C.DRX_TUNE1a_16MHZ_OP)
            ldecfg2.writeValue(C.LDE_CFG2_16MHZ_OP)
        elif pulseFrequency == C.TX_PULSE_FREQ_64MHZ:
            drxtune1a.writeValue(C.DRX_TUNE1a_64MHZ_OP)
            ldecfg2.writeValue(C.LDE_CFG2_64MHZ_OP)


    def tuneDrxtune1b(self, drxtune1b):
        """
        This function fills the array for the tuning of drxtune1b according to the datasheet and the enabled mode.

        Args:
            drxtune1b (DW1000Register): Register which will store the correct values for drxtune1b.
        """
        dataRate = self.operationMode[C.DATA_RATE_BIT]
        preambleLength = self.operationMode[C.PREAMBLE_LENGTH_BIT]
        if (preambleLength == C.TX_PREAMBLE_LEN_1536 or preambleLength == C.TX_PREAMBLE_LEN_2048 or preambleLength == C.TX_PREAMBLE_LEN_4096):
            if (dataRate == C.TRX_RATE_110KBPS):
                drxtune1b.writeValue(C.DRX_TUNE1b_M1024)
        elif preambleLength != C.TX_PREAMBLE_LEN_64:
            if (dataRate == C.TRX_RATE_850KBPS or dataRate == C.TRX_RATE_6800KBPS):
                drxtune1b.writeValue(C.DRX_TUNE1b_L1024)
        else:
            if dataRate == C.TRX_RATE_6800KBPS:
                drxtune1b(C.DRX_TUNE1b_64)


    def tuneDrxTune2(self, drxtune2):
        """
        This function fills the array for the tuning of drxtune2 according to the datasheet and the enabled mode.

        Args:
            drxtune2 (DW1000Register): Register which will store the correct values for drxtune2.
        """
        pacSize = self.operationMode[C.PAC_SIZE_BIT]
        pulseFrequency = self.operationMode[C.PULSE_FREQUENCY_BIT]
        if pacSize == C.PAC_SIZE_8:
            if pulseFrequency == C.TX_PULSE_FREQ_16MHZ:
                drxtune2.writeValue(C.DRX_TUNE2_8_16MHZ)
            elif pulseFrequency == C.TX_PULSE_FREQ_64MHZ:
                drxtune2.writeValue(C.DRX_TUNE2_8_64MHZ)
        elif pacSize == C.PAC_SIZE_16:
            if pulseFrequency == C.TX_PULSE_FREQ_16MHZ:
                drxtune2.writeValue(C.DRX_TUNE2_16_16MHZ)
            elif pulseFrequency == C.TX_PULSE_FREQ_64MHZ:
                drxtune2.writeValue(C.DRX_TUNE2_16_64MHZ)
        elif pacSize == C.PAC_SIZE_32:
            if pulseFrequency == C.TX_PULSE_FREQ_16MHZ:
                drxtune2.writeValue(C.DRX_TUNE2_32_16MHZ)
            elif pulseFrequency == C.TX_PULSE_FREQ_64MHZ:
                drxtune2.writeValue(C.DRX_TUNE2_32_64MHZ)
        elif pacSize == C.PAC_SIZE_64:
            if pulseFrequency == C.TX_PULSE_FREQ_16MHZ:
                drxtune2.writeValue(C.DRX_TUNE2_64_16MHZ)
            elif pulseFrequency == C.TX_PULSE_FREQ_64MHZ:
                drxtune2.writeValue(C.DRX_TUNE2_64_64MHZ)


    def tuneAccToChan(self, rftxctrl, tcpgdelay, fspllcfg, fsplltune, txpower):
        """
        This function fills the arrays for the tuning of rftxctrl, tcpgdelay, fspllcfg, fsplltune and txpower according to the datasheet and the enabled mode.

        Args:
            rftxctrl (DW1000Register): Register
            tcpgdelay (DW1000Register): Register
            fspllcfg (DW1000Register): Register
            fsplltune (DW1000Register): Register
            txpower (DW1000Register): Register
        """
        channel = self.operationMode[C.CHANNEL_BIT]
        pulseFrequency = self.operationMode[C.PULSE_FREQUENCY_BIT]
        if channel == C.CHANNEL_1:
            rftxctrl.writeValue(C.RF_TXCTRL_1)
            tcpgdelay.writeValue(C.TC_PGDELAY_1)
            fspllcfg.writeValue(C.FS_PLLCFG_1)
            fsplltune.writeValue(C.FS_PLLTUNE_1)
            if pulseFrequency == C.TX_PULSE_FREQ_16MHZ:
                txpower.writeValue(C.TX_POWER_12_16MHZ)
            elif pulseFrequency == C.TX_PULSE_FREQ_64MHZ:
                txpower.writeValue(C.TX_POWER_12_64MHZ)
        elif channel == C.CHANNEL_2:
            rftxctrl.writeValue(C.RF_TXCTRL_2)
            tcpgdelay.writeValue(C.TC_PGDELAY_2)
            fspllcfg.writeValue(C.FS_PLLCFG_24)
            fsplltune.writeValue(C.FS_PLLTUNE_24)
            if pulseFrequency == C.TX_PULSE_FREQ_16MHZ:
                txpower.writeValue(C.TX_POWER_12_16MHZ)
            elif pulseFrequency == C.TX_PULSE_FREQ_64MHZ:
                txpower.writeValue(C.TX_POWER_12_64MHZ)
        elif channel == C.CHANNEL_3:
            rftxctrl.writeValue(C.RF_TXCTRL_3)
            tcpgdelay.writeValue(C.TC_PGDELAY_3)
            fspllcfg.writeValue(C.FS_PLLCFG_3)
            fsplltune.writeValue(C.FS_PLLTUNE_3)
            if pulseFrequency == C.TX_PULSE_FREQ_16MHZ:
                txpower.writeValue(C.TX_POWER_3_16MHZ)
            elif pulseFrequency == C.TX_PULSE_FREQ_64MHZ:
                txpower.writeValue(C.TX_POWER_3_64MHZ)
        elif channel == C.CHANNEL_4:
            rftxctrl.writeValue(C.RF_TXCTRL_4)
            tcpgdelay.writeValue(C.TC_PGDELAY_4)
            fspllcfg.writeValue(C.FS_PLLCFG_24)
            fsplltune.writeValue(C.FS_PLLTUNE_24)
            if pulseFrequency == C.TX_PULSE_FREQ_16MHZ:
                txpower.writeValue(C.TX_POWER_4_16MHZ)
            elif pulseFrequency == C.TX_PULSE_FREQ_64MHZ:
                txpower.writeValue(C.TX_POWER_4_64MHZ)
        elif channel == C.CHANNEL_5:
            rftxctrl.writeValue(C.RF_TXCTRL_5)
            tcpgdelay.writeValue(C.TC_PGDELAY_5)
            fspllcfg.writeValue(C.FS_PLLCFG_57)
            fsplltune.writeValue(C.FS_PLLTUNE_57)
            if pulseFrequency == C.TX_PULSE_FREQ_16MHZ:
                txpower.writeValue(C.TX_POWER_5_16MHZ)
            elif pulseFrequency == C.TX_PULSE_FREQ_64MHZ:
                txpower.writeValue(C.TX_POWER_5_64MHZ)
        elif channel == C.CHANNEL_7:
            rftxctrl.writeValue(C.RF_TXCTRL_7)
            tcpgdelay.writeValue(C.TC_PGDELAY_7)
            fspllcfg.writeValue(C.FS_PLLCFG_57)
            fsplltune.writeValue(C.FS_PLLTUNE_57)
            if pulseFrequency == C.TX_PULSE_FREQ_16MHZ:
                txpower.writeValue(C.TX_POWER_7_16MHZ)
            elif pulseFrequency == C.TX_PULSE_FREQ_64MHZ:
                txpower.writeValue(C.TX_POWER_7_64MHZ)


    def tunelderepc(self, lderepc):
        """
        This function fills the arrays for the tuning of lderepc according to the datasheet and the enabled mode.

        Args:
            lderepc (DW1000Register): Register which will store the correct values for lderepC.
        """
        preacode = self.operationMode[C.PREAMBLE_CODE_BIT]
        dataRate = self.operationMode[C.DATA_RATE_BIT]
        if (preacode == C.PREAMBLE_CODE_16MHZ_1 or preacode == C.PREAMBLE_CODE_16MHZ_2):
            if dataRate == C.TRX_RATE_110KBPS:
                lderepc.writeValue((C.LDE_REPC_1AND2 >> 3) & C.MASK_LS_2BYTES)
            else:
                lderepc.writeValue(C.LDE_REPC_1AND2)
        elif (preacode == C.PREAMBLE_CODE_16MHZ_3 or preacode == C.PREAMBLE_CODE_16MHZ_8):
            if dataRate == C.TRX_RATE_110KBPS:
                lderepc.writeValue((C.LDE_REPC_3AND8 >> 3) & C.MASK_LS_2BYTES)
            else:
                lderepc.writeValue(C.LDE_REPC_3AND8)
        elif preacode == C.PREAMBLE_CODE_16MHZ_4:
            if dataRate == C.TRX_RATE_110KBPS:
                lderepc.writeValue((C.LDE_REPC_4 >> 3) & C.MASK_LS_2BYTES)
            else:
                lderepc.writeValue(C.LDE_REPC_4)
        elif preacode == C.PREAMBLE_CODE_16MHZ_5:
            if dataRate == C.TRX_RATE_110KBPS:
                lderepc.writeValue((C.LDE_REPC_5 >> 3) & C.MASK_LS_2BYTES)
            else:
                lderepc.writeValue(C.LDE_REPC_5)
        elif preacode == C.PREAMBLE_CODE_16MHZ_6:
            if dataRate == C.TRX_RATE_110KBPS:
                lderepc.writeValue((C.LDE_REPC_6 >> 3) & C.MASK_LS_2BYTES)
            else:
                lderepc.writeValue(C.LDE_REPC_6)
        elif preacode == C.PREAMBLE_CODE_16MHZ_7:
            if dataRate == C.TRX_RATE_110KBPS:
                lderepc.writeValue((C.LDE_REPC_7 >> 3) & C.MASK_LS_2BYTES)
            else:
                lderepc.writeValue(C.LDE_REPC_7)
        elif preacode == C.PREAMBLE_CODE_64MHZ_9:
            if dataRate == C.TRX_RATE_110KBPS:
                lderepc.writeValue((C.LDE_REPC_9 >> 3) & C.MASK_LS_2BYTES)
            else:
                lderepc.writeValue(C.LDE_REPC_9)
        elif (preacode == C.PREAMBLE_CODE_64MHZ_10 or preacode == C.PREAMBLE_CODE_64MHZ_17):
            if dataRate == C.TRX_RATE_110KBPS:
                lderepc.writeValue((C.LDE_REPC_1017 >> 3) & C.MASK_LS_2BYTES)
            else:
                lderepc.writeValue(C.LDE_REPC_1017)
        elif preacode == C.PREAMBLE_CODE_64MHZ_11:
            if dataRate == C.TRX_RATE_110KBPS:
                lderepc.writeValue((C.LDE_REPC_111321 >> 3) & C.MASK_LS_2BYTES)
            else:
                lderepc.writeValue(C.LDE_REPC_111321)
        elif preacode == C.PREAMBLE_CODE_64MHZ_12:
            if dataRate == C.TRX_RATE_110KBPS:
                lderepc.writeValue((C.LDE_REPC_12 >> 3) & C.MASK_LS_2BYTES)
            else:
                lderepc.writeValue(C.LDE_REPC_12)
        elif preacode == C.PREAMBLE_CODE_64MHZ_18 or preacode == C.PREAMBLE_CODE_64MHZ_19:
            if dataRate == C.TRX_RATE_110KBPS:
                lderepc.writeValue((C.LDE_REPC_14161819 >> 3) & C.MASK_LS_2BYTES)
            else:
                lderepc.writeValue(C.LDE_REPC_14161819)
        elif preacode == C.PREAMBLE_CODE_64MHZ_20:
            if dataRate == C.TRX_RATE_110KBPS:
                lderepc.writeValue((C.LDE_REPC_20 >> 3) & C.MASK_LS_2BYTES)
            else:
                lderepc.writeValue(C.LDE_REPC_20)


    """
    Message reception functions.
    """


    def newReceive(self):
        """
        This function prepares the chip for a new reception. It clears the system control register and also clear the RX latched bits in the SYS_STATUS register.
        """
        self.idle()
        self.sysctrl.clear()
        self.clearReceiveStatus()


    def startReceive(self):
        """
        This function configures the chip to start the reception of a message sent by another DW1000 chip.
        It turns on its receiver by setting RXENAB in the system control register.
        """
        self.sysctrl.setBit(C.RXENAB_BIT, True)
        self.writeRegister(self.sysctrl)


    def isReceiveFailed(self):
        """
        This function reads the system event status register and checks if the message reception failed.

        Returns:
            True if the reception failed. False otherwise.
        """
        val = self.sysstatus.getBitsOr([C.LDEERR_BIT, C.RXFCE_BIT, C.RXPHE_BIT, C.RXRFSL_BIT])
        if val:
            return True
        else:
            return False


    def isReceiveTimeout(self):
        """
        This function reads the system event status register and checks if there was a timeout in the message reception.

        Returns:
            True if there was a timeout in the reception. False otherwise.
        """
        isTimeout = self.sysstatus.getBit(C.RXRFTO_BIT)     \
                  | self.sysstatus.getBit(C.RXPTO_BIT)      \
                  | self.sysstatus.getBit(C.RXSFDTO_BIT)
        return isTimeout


    def clearReceiveStatus(self):
        """
        This function clears the system event status register at the bits related to the reception of a message.
        """
        self.clearStatus((C.RXDFR_BIT, C.LDEDONE_BIT
                                , C.LDEERR_BIT, C.RXPHE_BIT
                                , C.RXFCE_BIT, C.RXFCG_BIT
                                , C.RXRFSL_BIT))


    def getFirstPathPower(self):
        """
        This function calculates an estimate of the power in the first path signal. See section 4.7.1 of the DW1000 user manual for further details on the calculations.

        Returns:
            The estimated power in the first path signal.
        """
        fpAmpl1Bytes = DW1000Register(C.RX_TIME, C.FP_AMPL1_SUB, 2)
        fpAmpl2Bytes = DW1000Register(C.RX_FQUAL, C.FP_AMPL2_SUB, 2)
        fpAmpl3Bytes = DW1000Register(C.RX_FQUAL, C.PP_AMPL3_SUB, 2)
        rxFrameInfo = DW1000Register(C.RX_FINFO, C.NO_SUB, 4)
        self.readRegister(fpAmpl1Bytes)
        self.readRegister(fpAmpl2Bytes)
        self.readRegister(fpAmpl3Bytes)
        self.readRegister(rxFrameInfo)
        f1 = fpAmpl1Bytes[0] | fpAmpl1Bytes[1] << 8
        f2 = fpAmpl2Bytes[0] | fpAmpl2Bytes[1] << 8
        f3 = fpAmpl3Bytes[0] | fpAmpl3Bytes[1] << 8
        N = ((rxFrameInfo[2] >> 4) & C.MASK_LS_BYTE) | (rxFrameInfo[3] << 4)
        if self.operationMode[C.PULSE_FREQUENCY_BIT] == C.TX_PULSE_FREQ_16MHZ:
            A = C.A_16MHZ
            corrFac = C.CORRFAC_16MHZ
        else:
            A = C.A_64MHZ
            corrFac = C.CORRFAC_64MHZ
        estFPPower = C.PWR_COEFF2 * \
            math.log10((f1 * f1 + f2 * f2 + f3 * f3) / (N * N)) - A
        if estFPPower <= -C.PWR_COEFF:
            return estFPPower
        else:
            estFPPower += (estFPPower + C.PWR_COEFF) * corrFac
        return estFPPower


    def getReceivePower(self):
        """
        This function calculates an estimate of the receive power level. See section 4.7.2 of the DW1000 user manual for further details on the calculation.

        Returns:
            The estimated receive power for the current reception.
        """
        cirPwrBytes = DW1000Register(C.RX_FQUAL, C.CIR_PWR_SUB, 2)
        rxFrameInfo = DW1000Register(C.RX_FINFO, C.NO_SUB, 4)
        self.readRegister(cirPwrBytes)
        self.readRegister(rxFrameInfo)
        cir = cirPwrBytes[0] | cirPwrBytes[1] << 8
        N = ((rxFrameInfo[2] >> 4) & C.MASK_LS_BYTE) | rxFrameInfo[3] << 4
        if self.operationMode[C.PULSE_FREQUENCY_BIT] == C.TX_PULSE_FREQ_16MHZ:
            A = C.A_16MHZ
            corrFac = C.CORRFAC_16MHZ
        else:
            A = C.A_64MHZ
            corrFac = C.CORRFAC_64MHZ
        estRXPower = 0
        if ((float(cir) * float(C.TWOPOWER17)) / (float(N) * float(N)) > 0):
            estRXPower = C.PWR_COEFF2 * math.log10((float(cir) * float(C.TWOPOWER17)) / (float(N) * float(N))) - A
        if estRXPower <= -C.PWR_COEFF:
            return estRXPower
        else:
            estRXPower += (estRXPower + C.PWR_COEFF) * corrFac
        return estRXPower


    def getReceiveQuality(self):
        """
        This function calculates an estimate of the receive quality.abs

        Returns:
            The estimated receive quality for the current reception.
        """
        noiseBytes = DW1000Register(C.RX_FQUAL, C.STD_NOISE_SUB, 2)
        fpAmpl2Bytes = DW1000Register(C.RX_FQUAL, C.FP_AMPL2_SUB, 2)
        self.readRegister(noiseBytes)
        self.readRegister(fpAmpl2Bytes)
        noise = float(noiseBytes[0] | noiseBytes[1] << 8)
        f2 = float(fpAmpl2Bytes[0] | fpAmpl2Bytes[1] << 8)
        return f2 / noise


    def getReceiveTimestamp(self):
        """
        This function reads the receive timestamp from the register and returns it.

        Returns:
            The timestamp value of the last reception.
        """
        rxTimeBytes = DW1000Register(C.RX_TIME, C.RX_STAMP_SUB, 5)
        self.readRegister(rxTimeBytes)
        timestamp = 0
        for i in range(0, 5):
            timestamp |= rxTimeBytes[i] << (i * 8)
        timestamp = int(round(self.correctTimestamp(timestamp)))

        return timestamp


    def getReceiveFrameLength(self):
        """
        This function reads the length of the received frame. Used to read only the important parts of a transmission.

        Returns:
            Length of the frame in RX_BUFFER
        """
        self.readRegister(self.rxfinfo)

        return ((self.rxfinfo[1] & 0x3) << 8) | self.rxfinfo[0] # [RXFLE | RXFLEN]


    def correctTimestamp(self, timestamp):
        """
        This function corrects the timestamp read from the RX buffer.

        Args:
            timestamp: the timestamp you want to correct

        Returns:
            The corrected timestamp.
        """
        rxPowerBase = -(self.getReceivePower() + 61.0) * 0.5
        rxPowerBaseLow = int(math.floor(rxPowerBase))
        rxPowerBaseHigh = rxPowerBaseLow + 1

        if rxPowerBaseLow < 0:
            rxPowerBaseLow = 0
            rxPowerBaseHigh = 0
        elif rxPowerBaseHigh > 17:
            rxPowerBaseLow = 17
            rxPowerBaseHigh = 17

        if self.operationMode[C.CHANNEL_BIT] == C.CHANNEL_4 or self.operationMode[C.CHANNEL_BIT] == C.CHANNEL_7:
            if self.operationMode[C.PULSE_FREQUENCY_BIT] == C.TX_PULSE_FREQ_16MHZ:
                if rxPowerBaseHigh < C.BIAS_900_16_ZERO:
                    rangeBiasHigh = - C.BIAS_900_16[rxPowerBaseHigh]
                else:
                    rangeBiasHigh = C.BIAS_900_16[rxPowerBaseHigh]
                rangeBiasHigh <<= 1
                if rxPowerBaseLow < C.BIAS_900_16_ZERO:
                    rangeBiasLow = -C.BIAS_900_16[rxPowerBaseLow]
                else:
                    rangeBiasLow = C.BIAS_900_16[rxPowerBaseLow]
                rangeBiasLow <<= 1
            elif self.operationMode[C.PULSE_FREQUENCY_BIT] == C.TX_PULSE_FREQ_64MHZ:
                if rxPowerBaseHigh < C.BIAS_900_64_ZERO:
                    rangeBiasHigh = - C.BIAS_900_64[rxPowerBaseHigh]
                else:
                    rangeBiasHigh = C.BIAS_900_64[rxPowerBaseHigh]
                rangeBiasHigh <<= 1
                if rxPowerBaseLow < C.BIAS_900_64_ZERO:
                    rangeBiasLow = -C.BIAS_900_64[rxPowerBaseLow]
                else:
                    rangeBiasLow = C.BIAS_900_64[rxPowerBaseLow]
                rangeBiasLow <<= 1
        else:
            if self.operationMode[C.PULSE_FREQUENCY_BIT] == C.TX_PULSE_FREQ_16MHZ:
                if rxPowerBaseHigh < C.BIAS_500_16_ZERO:
                    rangeBiasHigh = - C.BIAS_500_16[rxPowerBaseHigh]
                else:
                    rangeBiasHigh = C.BIAS_500_16[rxPowerBaseHigh]
                rangeBiasHigh <<= 1
                if rxPowerBaseLow < C.BIAS_500_16_ZERO:
                    rangeBiasLow = -C.BIAS_500_16[rxPowerBaseLow]
                else:
                    rangeBiasLow = C.BIAS_500_16[rxPowerBaseLow]
                rangeBiasLow <<= 1
            elif self.operationMode[C.PULSE_FREQUENCY_BIT] == C.TX_PULSE_FREQ_64MHZ:
                if rxPowerBaseHigh < C.BIAS_500_64_ZERO:
                    rangeBiasHigh = - C.BIAS_500_64[rxPowerBaseHigh]
                else:
                    rangeBiasHigh = C.BIAS_500_64[rxPowerBaseHigh]
                rangeBiasHigh <<= 1
                if rxPowerBaseLow < C.BIAS_500_64_ZERO:
                    rangeBiasLow = -C.BIAS_500_64[rxPowerBaseLow]
                else:
                    rangeBiasLow = C.BIAS_500_64[rxPowerBaseLow]
                rangeBiasLow <<= 1

        rangeBias = rangeBiasLow + (rxPowerBase - rxPowerBaseLow) * (rangeBiasHigh - rangeBiasLow)
        adjustmentTimeTS = rangeBias * C.DISTANCE_OF_RADIO_INV * C.ADJUSTMENT_TIME_FACTOR
        timestamp += adjustmentTimeTS
        return timestamp


    def getMessage(self):
        """
        This function returns the most recent message in the receive buffer.
        """
        messageLen = self.getReceiveFrameLength()
        return bytearray(self.getData(messageLen))


    """
    Message transmission functions.
    """


    def newTransmit(self):
        """
        This function prepares the chip for a new transmission. It clears the system control register and also clears the TX latched bits in the SYS_STATUS register.
        """
        self.idle()
        self.sysctrl.clear()
        self.clearTransmitStatus()


    def startTransmit(self, wait4resp):
        """
        This function configures the chip to start the transmission of the message previously set in the TX register. It sets TXSTRT bit in the system control register to begin transmission.
        """
        self.writeRegister(self.txfctrl)
        self.sysctrl.setBit(C.WAIT4RESP_BIT, wait4resp)
        self.sysctrl.setBit(C.TXSTRT_BIT, True)
        self.writeRegister(self.sysctrl)


    def clearTransmitStatus(self):
        """
        This function clears the event status register at the bits related to the transmission of a message.
        """
        self.clearStatus((C.TXFRB_BIT, C.TXPRS_BIT, C.TXPHS_BIT, C.TXFRS_BIT))


    def setDelay(self, delay, unit, mode):
        """
        This function configures the chip to activate a delay between transmissions or receptions.

        Args:
            delay: The delay between each transmission/reception
            unit : The unit you want to put the delay in. Microseconds is the base unit.
            mode : "rx" | "tx"

        Returns:
            The timestamp's value with the added delay and antennaDelay.
        """
        if mode.lower() == "tx":
            self.sysctrl.setBit(C.TXDLYS_BIT, True)
        elif mode.lower() == "rx":
            self.sysctrl.setBit(C.RXDLYE_BIT, True)
        else:
            logging.error("Unknown mode")

        delayReg = DW1000Register(C.DX_TIME, C.NO_SUB, 5)
        sysTimeReg = DW1000Register(C.SYS_TIME, C.NO_SUB, 5)
        self.readRegister(sysTimeReg)
        futureTimeTS = 0
        for i in range(0, 5):
            futureTimeTS |= sysTimeReg[i] << (i * 8)
        futureTimeTS += int(delay * unit * C.TIME_RES_INV)

        delayReg.clear()
        for i in range(0, 5):
            delayReg[i] = int(futureTimeTS >> (i * 8)) & C.MASK_LS_BYTE

        delayReg[0] = 0
        delayReg[1] &= C.SET_DELAY_MASK
        self.writeRegister(delayReg)

        futureTimeTS = 0
        for i in range(0, 5):
            futureTimeTS |= delayReg[i] << (i * 8)

        futureTimeTS += C.ANTENNA_DELAY
        return futureTimeTS


    def setFrameWaitTimeout(self, waittime):
        """
        This function sets the Receiver Frame Wait Timeout Period. Use in conjunction with SYSCFG|RXWTOE (Receiver Wait Timeout Enable) and SYSSTATUS|RXRFTO.

        Args:
            waittime: The waittime in microseconds
        """
        rxfwto = DW1000Register(C.RX_FWTO, C.NO_SUB, 2)
        rxfwto.writeValue(waittime)
        self.writeRegister(rxfwto)


    def clearStatus(self, bits):
        """
        This function clears all specified bits in status register. Local status is invalid afterwards!

        Args:
            bits (array-like): List of bits to clear
        """
        self.sysstatus.clear()
        self.sysstatus.setBits(bits, True)
        self.writeRegister(self.sysstatus)


    def clearAllStatus(self):
        """
        This function clears all the status register by writing a 1 to every bits in it.
        """
        self.sysstatus.setAll(0xFF)
        self.writeRegister(self.sysstatus)


    def getTransmitTimestamp(self):
        """
        This function reads the transmit timestamp from the register and returns it.

        Returns:
            The timestamp value of the last transmission.
        """
        txTimeRegister = DW1000Register(C.TX_TIME, C.TX_STAMP_SUB, 5)
        self.readRegister(txTimeRegister)
        timeStamp = 0
        for i in range(0, 5):
            timeStamp |= int(txTimeRegister[i] << (i * 8))
        return timeStamp


    def setTimeStamp(self, data, timeStamp, index):
        """
        This function sets the specified timestamp into the data that will be sent.

        Args:
            data: The data where you will store the timestamp
            timeStamp = The timestamp's value
            index = The bit from where you will put the timestamp's value

        Returns:
            The data with the timestamp added to it
        """
        for i in range(0, 5):
            data[i+index] = int((timeStamp >> (i * 8)) & C.MASK_LS_BYTE)


    def getTimeStamp(self, data, index):
        """
        This function gets the timestamp's value written inside the specified data and returns it.

        Args:
            data : the data where you want to extract the timestamp from
            index : the index you want to start reading the data from

        Returns:
            The timestamp's value read from the given data.
        """
        timestamp = 0
        for i in range(0, 5):
            timestamp |= int(data[i+index]) << (i*8)
        return timestamp


    def wrapTimestamp(self, timestamp):
        """
        This function converts the negative values of the timestamp due to the overflow into a correct one.

        Args:
            timestamp : the timestamp's value you want to correct.

        Returns:
            The corrected timestamp's value.
        """
        if timestamp < 0:
            timestamp += C.TIME_OVERFLOW
        return timestamp


    def sendMessage(self, dstAddr, dstPAN, payload, ackReq=True, wait4resp=True, delay=None):
        """
        This function sends a message to the specified address and network. Uses 802.15.4a headers in packets.

        Args:
            dstAddr: Short ID of the destination
            dstPAN: Network identifier of the destination
            payload (bytes): Data to be send

        Kwargs:
            ackReq: Request receiver to reply with acknowledge frame
            wait4resp: Immediately turn on receiver after send
            delay: Time in microseconds
        """
        srcAddr = self.panadr[0:2]

        # Fill header structures
        header = MAC.MACHeader()
        header.frameControl.frameType = MAC.FT_DATA
        header.frameControl.secEnable = 0
        header.frameControl.framePending = 0 # TODO: Split larger transmission
        header.frameControl.ackRequest = ackReq
        header.frameControl.panCompression = 1 # Only send destination PAN address
        header.frameControl.destAddrMode = MAC.AD_SAD # Short address
        header.frameControl.frameVersion = MAC.IEEE802_15_4_2003
        header.frameControl.srcAddrMode = MAC.AD_SAD # Short address

        header.seqNumber = self.seqNum
        header.destPAN = dstPAN
        header.destAddr = dstAddr
        header.srcAddr = srcAddr
        # CRC is auto calculated by DW1000 (SFCST_BIT = False)

        headerEnc = header.encode()

        message = bytearray(headerEnc + payload)

        self.newTransmit()
        self.setData(message, len(message)) # The TX frame length will be set to len(message) + 2 on the chip to include CRC
        if delay:
            self.setDelay(delay, C.MICROSECONDS, "tx")
        self.startTransmit(wait4resp)

        self.seqNum = (self.seqNum + 1) % 256


    """
    Data functions
    """


    def getDataStr(self):
        """
        This function reads the RX buffer register to process the received message. First, it reads the length of the message in the RX frame info register, then
        read the RX buffer register and converts the ASCII values into strings.

        Returns:
            The data in the RX buffer as a string.
        """
        rxFrameInfoReg = DW1000Register(C.RX_FINFO, C.NO_SUB, 4)
        self.readRegister(rxFrameInfoReg)
        len = (((rxFrameInfoReg[1] << 8) | rxFrameInfoReg[0]) & C.GET_DATA_MASK)
        len = len - 2

        dataBytes = self.getData(len)
        dataString = "".join(chr(i) for i in dataBytes)
        return dataString


    def getData(self, dataLength):
        """
        This function reads a number of bytes in the RX buffer register, stores it into an array and return it.

        Args:
            dataLength: The number of bytes you want to read from the rx buffer.

        Returns:
            The data read in the RX buffer as a bytearray.
        """
        data = bytearray(dataLength)
        self.readBytes(C.RX_BUFFER, C.NO_SUB, data, dataLength)
        return data


    def setDataStr(self, data):
        """
        This function converts the specified string into an array of bytes and calls the setData function.

        Args:
            data: The string message the transmitter will send.
        """
        dataLength = len(data) + 1
        testData = bytearray(dataLength)
        for i in range(0, len(data)):
            testData[i] = ord(data[i])
        self.setData(testData, dataLength)


    def setData(self, data, dataLength):
        """
        This function writes the byte array into the TX buffer register to store the message which will be sent.

        Args:
            data: the byte array which contains the data to be written in the register
            dataLength: The size of the data which will be sent.
        """
        self.writeBytes(C.TX_BUFFER, C.NO_SUB, data, dataLength)
        self.readBytes(C.TX_BUFFER, C.NO_SUB, data, dataLength)
        dataLength += 2  # _frameCheck true, two bytes CRC
        self.txfctrl[0] = (dataLength & C.MASK_LS_BYTE)
        self.txfctrl[1] &= C.SET_DATA_MASK1
        self.txfctrl[1] |= ((dataLength >> 8) & C.SET_DATA_MASK2)


    def getDeviceModeInfoString(self):
        """
        This function returns the various device mode operating informations such as datarate, pulse frequency, the channel used, etc

        Returns:
            String containing device mode information
        """
        try:
            prf = C.PFREQ_DICT[self.operationMode[C.PULSE_FREQUENCY_BIT]]
        except KeyError:
            prf = 0

        try:
            plen = C.PLEN_DICT[self.operationMode[C.PREAMBLE_LENGTH_BIT]]
        except KeyError:
            plen = 0

        try:
            dr = C.DATA_RATE_DICT[self.operationMode[C.DATA_RATE_BIT]]
        except KeyError:
            dr = 0

        ch = self.operationMode[C.CHANNEL_BIT]
        pcode = self.operationMode[C.PREAMBLE_CODE_BIT]

        return "Device mode: Data rate {} kb/s, PRF : {} MHz, Preamble: {} symbols (code {}), Channel : {}".format(dr, prf, plen, pcode, ch)


    def readBytesOTP(self, address, data):
        """
        This function reads a value from the OTP memory following 6.3.3 table 13 of the user manual

        Args:
            address: The address to read in the OTP register.
            data: The data that will store the value read.
        """
        addressBytes = bytearray(2)
        addressBytes[0] = address & C.MASK_LS_BYTE
        addressBytes[1] = (address >> 8) & C.MASK_LS_BYTE
        self.writeBytes(C.OTP_IF, C.OTP_ADDR_SUB, addressBytes, 2)
        self.writeBytes(C.OTP_IF, C.OTP_CTRL_SUB, bytearray([C.OTP_STEP2]), 1)
        self.writeBytes(C.OTP_IF, C.OTP_CTRL_SUB, bytearray([C.OTP_STEP3]), 1)
        self.readBytes(C.OTP_IF, C.OTP_RDAT_SUB, data, 4)
        self.writeBytes(C.OTP_IF, C.OTP_CTRL_SUB, bytearray([C.OTP_STEP5]), 1)
        return data
