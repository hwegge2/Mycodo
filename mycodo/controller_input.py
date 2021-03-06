# coding=utf-8
#
# controller_input.py - Input controller that manages reading inputs and
#                       creating database entries
#
#  Copyright (C) 2017  Kyle T. Gabriel
#
#  This file is part of Mycodo
#
#  Mycodo is free software: you can redistribute it and/or modify
#  it under the terms of the GNU General Public License as published by
#  the Free Software Foundation, either version 3 of the License, or
#  (at your option) any later version.
#
#  Mycodo is distributed in the hope that it will be useful,
#  but WITHOUT ANY WARRANTY; without even the implied warranty of
#  MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the
#  GNU General Public License for more details.
#
#  You should have received a copy of the GNU General Public License
#  along with Mycodo. If not, see <http://www.gnu.org/licenses/>.
#
#  Contact at kylegabriel.com

import datetime
import logging
import threading
import time
import timeit

import RPi.GPIO as GPIO
import fasteners
import requests

from mycodo.config import LIST_DEVICES_I2C
from mycodo.databases.models import Conditional
from mycodo.databases.models import Input
from mycodo.databases.models import Output
from mycodo.databases.models import SMTP
from mycodo.devices.ads1x15 import ADS1x15Read
from mycodo.devices.mcp342x import MCP342xRead
from mycodo.devices.tca9548a import TCA9548A
from mycodo.inputs.am2315 import AM2315Sensor
from mycodo.inputs.atlas_ph import AtlaspHSensor
from mycodo.inputs.atlas_pt1000 import AtlasPT1000Sensor
from mycodo.inputs.bh1750 import BH1750Sensor
from mycodo.inputs.bme280 import BME280Sensor
from mycodo.inputs.bmp180 import BMP180Sensor
from mycodo.inputs.bmp280 import BMP280Sensor
from mycodo.inputs.chirp import ChirpSensor
from mycodo.inputs.dht11 import DHT11Sensor
from mycodo.inputs.dht22 import DHT22Sensor
from mycodo.inputs.ds18b20 import DS18B20Sensor
from mycodo.inputs.gpio_state import GPIOState
from mycodo.inputs.htu21d import HTU21DSensor
from mycodo.inputs.k30 import K30Sensor
from mycodo.inputs.linux_command import LinuxCommand
from mycodo.inputs.mh_z16 import MHZ16Sensor
from mycodo.inputs.mh_z19 import MHZ19Sensor
from mycodo.inputs.mycodo_ram import MycodoRam
from mycodo.inputs.raspi import RaspberryPiCPUTemp
from mycodo.inputs.raspi_cpuload import RaspberryPiCPULoad
from mycodo.inputs.raspi_freespace import RaspberryPiFreeSpace
from mycodo.inputs.server_ping import ServerPing
from mycodo.inputs.server_port_open import ServerPortOpen
from mycodo.inputs.sht1x_7x import SHT1x7xSensor
from mycodo.inputs.sht2x import SHT2xSensor
from mycodo.inputs.signal_pwm import SignalPWMInput
from mycodo.inputs.signal_rpm import SignalRPMInput
from mycodo.inputs.tmp006 import TMP006Sensor
from mycodo.inputs.tsl2561 import TSL2561Sensor
from mycodo.inputs.tsl2591_sensor import TSL2591Sensor
from mycodo.mycodo_client import DaemonControl
from mycodo.utils.database import db_retrieve_table_daemon
from mycodo.utils.influx import add_measure_influxdb
from mycodo.utils.influx import write_influxdb_value


class Measurement:
    """
    Class for holding all measurement values in a dictionary.
    The dictionary is formatted in the following way:

    {'measurement type':measurement value}

    Measurement type: The environmental or physical condition
    being measured, such as 'temperature', or 'pressure'.

    Measurement value: The actual measurement of the condition.
    """

    def __init__(self, raw_data):
        self.rawData = raw_data

    @property
    def values(self):
        return self.rawData


class InputController(threading.Thread):
    """
    Class for controlling the input

    """
    def __init__(self, ready, input_id):
        threading.Thread.__init__(self)

        self.logger = logging.getLogger(
            "mycodo.input_{id}".format(id=input_id))

        self.stop_iteration_counter = 0
        self.thread_startup_timer = timeit.default_timer()
        self.thread_shutdown_timer = 0
        self.ready = ready
        self.lock = {}
        self.measurement = None
        self.updateSuccess = False
        self.input_id = input_id
        self.control = DaemonControl()
        self.pause_loop = False
        self.verify_pause_loop = True

        input_dev = db_retrieve_table_daemon(Input, device_id=self.input_id)
        self.input_sel = input_dev
        self.id = input_dev.id
        self.unique_id = input_dev.unique_id
        self.i2c_bus = input_dev.i2c_bus
        self.location = input_dev.location
        self.power_output_id = input_dev.power_relay_id
        self.measurements = input_dev.measurements
        self.device = input_dev.device
        self.interface = input_dev.interface
        self.device_loc = input_dev.device_loc
        self.baud_rate = input_dev.baud_rate
        self.period = input_dev.period
        self.resolution = input_dev.resolution
        self.sensitivity = input_dev.sensitivity
        self.cmd_command = input_dev.cmd_command
        self.cmd_measurement = input_dev.cmd_measurement
        self.cmd_measurement_units = input_dev.cmd_measurement_units
        self.mux_address_raw = input_dev.multiplexer_address
        self.mux_bus = input_dev.multiplexer_bus
        self.mux_chan = input_dev.multiplexer_channel
        self.adc_chan = input_dev.adc_channel
        self.adc_gain = input_dev.adc_gain
        self.adc_resolution = input_dev.adc_resolution
        self.adc_measure = input_dev.adc_measure
        self.adc_measure_units = input_dev.adc_measure_units
        self.adc_volts_min = input_dev.adc_volts_min
        self.adc_volts_max = input_dev.adc_volts_max
        self.adc_units_min = input_dev.adc_units_min
        self.adc_units_max = input_dev.adc_units_max
        self.adc_inverse_unit_scale = input_dev.adc_inverse_unit_scale
        self.sht_clock_pin = input_dev.sht_clock_pin
        self.sht_voltage = input_dev.sht_voltage

        # Edge detection
        self.switch_edge = input_dev.switch_edge
        self.switch_bouncetime = input_dev.switch_bouncetime
        self.switch_reset_period = input_dev.switch_reset_period

        # PWM and RPM options
        self.weighting = input_dev.weighting
        self.rpm_pulses_per_rev = input_dev.rpm_pulses_per_rev
        self.sample_time = input_dev.sample_time

        # Server options
        self.port = input_dev.port
        self.times_check = input_dev.times_check
        self.deadline = input_dev.deadline

        # Output that will activate prior to input read
        self.pre_output_id = input_dev.pre_relay_id
        self.pre_output_duration = input_dev.pre_relay_duration
        self.pre_output_setup = False
        self.next_measurement = time.time()
        self.get_new_measurement = False
        self.trigger_cond = False
        self.measurement_acquired = False
        self.pre_output_activated = False
        self.pre_output_timer = time.time()

        output = db_retrieve_table_daemon(Output, entry='all')
        for each_output in output:  # Check if output ID actually exists
            if each_output.id == self.pre_output_id and self.pre_output_duration:
                self.pre_output_setup = True

        smtp = db_retrieve_table_daemon(SMTP, entry='first')
        self.smtp_max_count = smtp.hourly_max
        self.email_count = 0
        self.allowed_to_send_notice = True

        # Convert string I2C address to base-16 int
        if self.device in LIST_DEVICES_I2C:
            self.i2c_address = int(str(self.location), 16)

        # Set up multiplexer if enabled
        if self.device in LIST_DEVICES_I2C and self.mux_address_raw:
            self.mux_address_string = self.mux_address_raw
            self.mux_address = int(str(self.mux_address_raw), 16)
            self.mux_lock = "/var/lock/mycodo_multiplexer_0x{i2c:02X}.pid".format(
                i2c=self.mux_address)
            self.mux_lock = fasteners.InterProcessLock(self.mux_lock)
            self.mux_lock_acquired = False
            self.multiplexer = TCA9548A(self.mux_bus, self.mux_address)
        else:
            self.multiplexer = None

        # Set up edge detection of a GPIO pin
        if self.device == 'EDGE':
            if self.switch_edge == 'rising':
                self.switch_edge_gpio = GPIO.RISING
            elif self.switch_edge == 'falling':
                self.switch_edge_gpio = GPIO.FALLING
            else:
                self.switch_edge_gpio = GPIO.BOTH

        # Lock multiplexer, if it's enabled
        if self.multiplexer:
            self.lock_multiplexer()

        # Set up analog-to-digital converter
        if self.device in ['ADS1x15', 'MCP342x'] and self.location:
            self.adc_lock_file = "/var/lock/mycodo_adc_bus{bus}_0x{i2c:02X}.pid".format(
                bus=self.i2c_bus, i2c=self.i2c_address)

            if self.device == 'ADS1x15':
                self.adc = ADS1x15Read(self.i2c_address, self.i2c_bus,
                                       self.adc_chan, self.adc_gain)
            elif self.device == 'MCP342x':
                self.adc = MCP342xRead(self.i2c_address, self.i2c_bus,
                                       self.adc_chan, self.adc_gain,
                                       self.adc_resolution)
        else:
            self.adc = None

        self.device_recognized = True

        # Set up inputs or devices
        if self.device in ['EDGE', 'ADS1x15', 'MCP342x']:
            self.measure_input = None
        elif self.device == 'MYCODO_RAM':
            self.measure_input = MycodoRam()
        elif self.device == 'RPiCPULoad':
            self.measure_input = RaspberryPiCPULoad()
        elif self.device == 'RPi':
            self.measure_input = RaspberryPiCPUTemp()
        elif self.device == 'RPiFreeSpace':
            self.measure_input = RaspberryPiFreeSpace(self.location)
        elif self.device == 'AM2302':
            self.measure_input = DHT22Sensor(int(self.location))
        elif self.device == 'AM2315':
            self.measure_input = AM2315Sensor(self.i2c_bus,
                                              power=self.power_output_id)
        elif self.device == 'ATLAS_PH_I2C':
            self.measure_input = AtlaspHSensor(self.interface,
                                               i2c_address=self.i2c_address,
                                               i2c_bus=self.i2c_bus,
                                               sensor_sel=self.input_sel)
        elif self.device == 'ATLAS_PH_UART':
            self.measure_input = AtlaspHSensor(self.interface,
                                               device_loc=self.device_loc,
                                               baud_rate=self.baud_rate,
                                               sensor_sel=self.input_sel)
        elif self.device == 'ATLAS_PT1000_I2C':
            self.measure_input = AtlasPT1000Sensor(self.interface,
                                                   i2c_address=self.i2c_address,
                                                   i2c_bus=self.i2c_bus)
        elif self.device == 'ATLAS_PT1000_UART':
            self.measure_input = AtlasPT1000Sensor(self.interface,
                                                   device_loc=self.device_loc,
                                                   baud_rate=self.baud_rate)
        elif self.device == 'BH1750':
            self.measure_input = BH1750Sensor(self.i2c_address,
                                              self.i2c_bus,
                                              self.resolution,
                                              self.sensitivity)
        elif self.device == 'BME280':
            self.measure_input = BME280Sensor(self.i2c_address,
                                              self.i2c_bus)
        elif self.device == 'BMP180':
            self.measure_input = BMP180Sensor(self.i2c_bus)
        elif self.device == 'BMP280':
            self.measure_input = BMP280Sensor(self.i2c_address,
                                              self.i2c_bus)
        elif self.device == 'CHIRP':
            self.measure_input = ChirpSensor(self.i2c_address,
                                             self.i2c_bus)
        elif self.device == 'DS18B20':
            self.measure_input = DS18B20Sensor(self.location)
        elif self.device == 'DHT11':
            self.measure_input = DHT11Sensor(self.input_id,
                                             int(self.location),
                                             power=self.power_output_id)
        elif self.device == 'DHT22':
            self.measure_input = DHT22Sensor(int(self.location),
                                             power=self.power_output_id)
        elif self.device == 'GPIO_STATE':
            self.measure_input = GPIOState(int(self.location))
        elif self.device == 'HTU21D':
            self.measure_input = HTU21DSensor(self.i2c_bus)
        elif self.device == 'K30_UART':
            self.measure_input = K30Sensor(self.device_loc,
                                           baud_rate=self.baud_rate)
        elif self.device == 'MH_Z16_I2C':
            self.measure_input = MHZ16Sensor(self.interface,
                                             i2c_address=self.i2c_address,
                                             i2c_bus=self.i2c_bus)
        elif self.device == 'MH_Z16_UART':
            self.measure_input = MHZ16Sensor(self.interface,
                                             device_loc=self.device_loc,
                                             baud_rate=self.baud_rate)
        elif self.device == 'MH_Z19_UART':
            self.measure_input = MHZ19Sensor(self.device_loc,
                                             baud_rate=self.baud_rate)
        elif self.device == 'SHT1x_7x':
            self.measure_input = SHT1x7xSensor(int(self.location),
                                               self.sht_clock_pin,
                                               self.sht_voltage)
        elif self.device == 'SHT2x':
            self.measure_input = SHT2xSensor(self.i2c_address,
                                             self.i2c_bus)
        elif self.device == 'SIGNAL_PWM':
            self.measure_input = SignalPWMInput(int(self.location),
                                                self.weighting,
                                                self.sample_time)
        elif self.device == 'SIGNAL_RPM':
            self.measure_input = SignalRPMInput(int(self.location),
                                                self.weighting,
                                                self.rpm_pulses_per_rev,
                                                self.sample_time)
        elif self.device == 'TMP006':
            self.measure_input = TMP006Sensor(self.i2c_address,
                                              self.i2c_bus)
        elif self.device == 'TSL2561':
            self.measure_input = TSL2561Sensor(self.i2c_address,
                                               self.i2c_bus)
        elif self.device == 'TSL2591':
            self.measure_input = TSL2591Sensor(self.i2c_address,
                                               self.i2c_bus)
        elif self.device == 'LinuxCommand':
            self.measure_input = LinuxCommand(self.cmd_command,
                                              self.cmd_measurement)
        elif self.device == 'SERVER_PING':
            self.measure_input = ServerPing(self.location,
                                            self.times_check,
                                            self.deadline)
        elif self.device == 'SERVER_PORT_OPEN':
            self.measure_input = ServerPortOpen(self.location,
                                                self.port)
        else:
            self.device_recognized = False
            self.logger.debug("Device '{device}' not recognized".format(
                device=self.device))
            raise Exception("'{device}' is not a valid device type.".format(
                device=self.device))

        if self.multiplexer:
            self.unlock_multiplexer()

        self.edge_reset_timer = time.time()
        self.input_timer = time.time()
        self.running = False
        self.lastUpdate = None

    def run(self):
        try:
            self.running = True
            self.logger.info("Activated in {:.1f} ms".format(
                (timeit.default_timer() - self.thread_startup_timer) * 1000))
            self.ready.set()

            # Set up edge detection
            if self.device == 'EDGE':
                GPIO.setmode(GPIO.BCM)
                GPIO.setup(int(self.location), GPIO.IN)
                GPIO.add_event_detect(int(self.location),
                                      self.switch_edge_gpio,
                                      callback=self.edge_detected,
                                      bouncetime=self.switch_bouncetime)

            while self.running:
                # Pause loop to modify conditional statements.
                # Prevents execution of conditional while variables are
                # being modified.
                if self.pause_loop:
                    self.verify_pause_loop = True
                    while self.pause_loop:
                        time.sleep(0.1)

                if self.device not in ['EDGE']:
                    now = time.time()
                    # Signal that a measurement needs to be obtained
                    if now > self.next_measurement and not self.get_new_measurement:
                        self.get_new_measurement = True
                        self.trigger_cond = True
                        while self.next_measurement < now:
                            self.next_measurement += self.period

                    # if signaled and a pre output is set up correctly, turn the
                    # output on for the set duration
                    if (self.get_new_measurement and
                            self.pre_output_setup and
                            not self.pre_output_activated):
                        self.pre_output_timer = now + self.pre_output_duration
                        self.pre_output_activated = True

                        output_on = threading.Thread(
                            target=self.control.relay_on,
                            args=(self.pre_output_id,
                                  self.pre_output_duration,))
                        output_on.start()

                    # If using a pre output, wait for it to complete before
                    # querying the input for a measurement
                    if self.get_new_measurement:
                        if ((self.pre_output_setup and
                                self.pre_output_activated and
                                now < self.pre_output_timer) or
                                not self.pre_output_setup):
                            # Get measurement(s) from input
                            self.update_measure()
                            # Add measurement(s) to influxdb
                            if self.updateSuccess:
                                add_measure_influxdb(self.unique_id, self.measurement)
                            self.pre_output_activated = False
                            self.get_new_measurement = False

                self.trigger_cond = False

                time.sleep(0.1)

            self.running = False

            if self.device == 'EDGE':
                GPIO.setmode(GPIO.BCM)
                GPIO.cleanup(int(self.location))

            self.logger.info("Deactivated in {:.1f} ms".format(
                (timeit.default_timer() - self.thread_shutdown_timer) * 1000))
        except requests.ConnectionError:
            self.logger.error("Could not connect to influxdb. Check that it "
                              "is running and accepting connections")
        except Exception as except_msg:
            self.logger.exception("Error: {err}".format(
                err=except_msg))

    def lock_multiplexer(self):
        """ Acquire a multiplexer lock """
        self.mux_lock_acquired = False

        for _ in range(600):
            self.mux_lock_acquired = self.mux_lock.acquire(blocking=False)
            if self.mux_lock_acquired:
                break
            else:
                time.sleep(0.1)

        if not self.mux_lock_acquired:
            self.logger.error(
                "Unable to acquire lock: {lock}".format(lock=self.mux_lock))

        self.logger.debug(
            "Setting multiplexer ({add}) to channel {chan}".format(
                add=self.mux_address_string,
                chan=self.mux_chan))

        # Set multiplexer channel
        (multiplexer_status,
         multiplexer_response) = self.multiplexer.setup(self.mux_chan)

        if not multiplexer_status:
            self.logger.warning(
                "Could not set channel with multiplexer at address {add}."
                " Error: {err}".format(
                    add=self.mux_address_string,
                    err=multiplexer_response))
            self.updateSuccess = False
            return 1

    def unlock_multiplexer(self):
        """ Remove a multiplexer lock """
        if self.mux_lock and self.mux_lock_acquired:
            self.mux_lock.release()

    def read_adc(self):
        """ Read voltage from ADC """
        try:
            lock_acquired = False
            adc_lock = fasteners.InterProcessLock(self.adc_lock_file)
            for _ in range(600):
                lock_acquired = adc_lock.acquire(blocking=False)
                if lock_acquired:
                    break
                else:
                    time.sleep(0.1)
            if not lock_acquired:
                self.logger.error(
                    "Unable to acquire lock: {lock}".format(
                        lock=self.adc_lock_file))

            # Get measurement from ADC
            measurements = self.adc.next()

            if measurements is not None:
                # Get the voltage difference between min and max volts
                diff_voltage = abs(self.adc_volts_max - self.adc_volts_min)
                # Ensure the voltage stays within the min/max bounds
                if measurements['voltage'] < self.adc_volts_min:
                    measured_voltage = self.adc_volts_min
                elif measurements['voltage'] > self.adc_volts_max:
                    measured_voltage = self.adc_volts_max
                else:
                    measured_voltage = measurements['voltage']
                # Calculate the percentage of the voltage difference
                percent_diff = ((measured_voltage - self.adc_volts_min) /
                                diff_voltage)

                # Get the units difference between min and max units
                diff_units = abs(self.adc_units_max - self.adc_units_min)
                # Calculate the measured units from the percent difference
                if self.adc_inverse_unit_scale:
                    converted_units = (self.adc_units_max -
                                       (diff_units * percent_diff))
                else:
                    converted_units = (self.adc_units_min +
                                       (diff_units * percent_diff))
                # Ensure the units stay within the min/max bounds
                if converted_units < self.adc_units_min:
                    measurements[self.adc_measure] = self.adc_units_min
                elif converted_units > self.adc_units_max:
                    measurements[self.adc_measure] = self.adc_units_max
                else:
                    measurements[self.adc_measure] = converted_units

                if adc_lock and lock_acquired:
                    adc_lock.release()

                return measurements

        except Exception as except_msg:
            self.logger.exception(
                "Error while attempting to read adc: {err}".format(
                    err=except_msg))

        return None

    def update_measure(self):
        """
        Retrieve measurement from input

        :return: None if success, 0 if fail
        :rtype: int or None
        """
        measurements = None

        if not self.device_recognized:
            self.logger.debug("Device not recognized: {device}".format(
                device=self.device))
            self.updateSuccess = False
            return 1

        # Lock multiplexer, if it's enabled
        if self.multiplexer:
            self.lock_multiplexer()

        if self.adc:
            measurements = self.read_adc()
        else:
            try:
                # Get measurement from input
                measurements = self.measure_input.next()
                # Reset StopIteration counter on successful read
                if self.stop_iteration_counter:
                    self.stop_iteration_counter = 0
            except StopIteration:
                self.stop_iteration_counter += 1
                # Notify after 3 consecutive errors. Prevents filling log
                # with many one-off errors over long periods of time
                if self.stop_iteration_counter > 2:
                    self.stop_iteration_counter = 0
                    self.logger.error(
                        "StopIteration raised. Possibly could not read "
                        "input. Ensure it's connected properly and "
                        "detected.")
            except Exception as except_msg:
                self.logger.exception(
                    "Error while attempting to read input: {err}".format(
                        err=except_msg))

        if self.multiplexer:
            self.unlock_multiplexer()

        if self.device_recognized and measurements is not None:
            self.measurement = Measurement(measurements)
            self.updateSuccess = True
        else:
            self.updateSuccess = False

        self.lastUpdate = time.time()

    def edge_detected(self, bcm_pin):
        """
        Callback function from GPIO.add_event_detect() for when an edge is detected

        Write rising (1) or falling (-1) edge to influxdb database
        Trigger any conditionals that match the rising/falling/both edge

        :param bcm_pin: BMC pin of rising/falling edge (required parameter)
        :return: None
        """
        gpio_state = GPIO.input(int(self.location))
        if time.time() > self.edge_reset_timer:
            self.edge_reset_timer = time.time()+self.switch_reset_period

            if (self.switch_edge == 'rising' or
                    (self.switch_edge == 'both' and gpio_state)):
                rising_or_falling = 1  # Rising edge detected
                state_str = 'Rising'
                conditional_edge = 1
            else:
                rising_or_falling = -1  # Falling edge detected
                state_str = 'Falling'
                conditional_edge = 0

            write_db = threading.Thread(
                target=write_influxdb_value,
                args=(self.unique_id, 'edge', rising_or_falling,))
            write_db.start()

            conditionals = db_retrieve_table_daemon(Conditional)
            conditionals = conditionals.filter(
                Conditional.conditional_type == 'conditional_edge')
            conditionals = conditionals.filter(
                Conditional.if_sensor_measurement == self.unique_id)
            conditionals = conditionals.filter(
                Conditional.is_activated == True)

            for each_conditional in conditionals.all():
                if each_conditional.if_sensor_edge_detected in ['both', state_str.lower()]:
                    now = time.time()
                    timestamp = datetime.datetime.fromtimestamp(
                        now).strftime('%Y-%m-%d %H-%M-%S')
                    message = "{ts}\n[Conditional {cid} ({cname})] " \
                              "Input {oid} ({name}) {state} edge detected " \
                              "on pin {pin} (BCM)".format(
                        ts=timestamp,
                        cid=each_conditional.id,
                        cname=each_conditional.name,
                        name=each_conditional.name,
                        oid=self.id,
                        state=state_str,
                        pin=bcm_pin)

                    self.control.trigger_conditional_actions(
                        each_conditional.id, message=message,
                        edge=conditional_edge)

    def is_running(self):
        return self.running

    def stop_controller(self):
        self.thread_shutdown_timer = timeit.default_timer()
        if self.device not in ['EDGE', 'ADS1x15', 'MCP342x']:
            self.measure_input.stop_sensor()
        self.running = False
