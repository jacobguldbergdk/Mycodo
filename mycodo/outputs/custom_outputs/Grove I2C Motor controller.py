# coding=utf-8
#
# grove_i2c_motor.py - Output for Grove I2C motor controller
#
import copy
import threading
import time

from flask_babel import lazy_gettext

from mycodo.databases.models import OutputChannel
from mycodo.outputs.base_output import AbstractOutput
from mycodo.utils.database import db_retrieve_table_daemon
from mycodo.utils.influx import add_measurements_influxdb


def constraints_pass_positive_value(mod_input, value):
    """
    Check if the user input is acceptable
    :param mod_input: SQL object with user-saved Input options
    :param value: float or int
    :return: tuple: (bool, list of strings)
    """
    errors = []
    all_passed = True
    # Ensure value is positive
    if value <= 0:
        all_passed = False
        errors.append("Must be a positive value")
    return all_passed, errors, mod_input


def constraints_pass_positive_or_zero_value(mod_input, value):
    """
    Check if the user input is acceptable
    :param mod_input: SQL object with user-saved Input options
    :param value: float or int
    :return: tuple: (bool, list of strings)
    """
    errors = []
    all_passed = True
    # Ensure value is positive
    if value < 0:
        all_passed = False
        errors.append("Must be a positive value")
    return all_passed, errors, mod_input


def constraints_pass_percent(mod_input, value):
    """
    Check if the user input is acceptable
    :param mod_input: SQL object with user-saved Input options
    :param value: float or int
    :return: tuple: (bool, list of strings)
    """
    errors = []
    all_passed = True
    # Ensure value is positive
    if 100 < value <= 0:
        all_passed = False
        errors.append("Must be a positive value")
    return all_passed, errors, mod_input


# Measurements
measurements_dict = {
    0: {
        'measurement': 'duration_time',
        'unit': 's'
    },
    1: {
        'measurement': 'volume',
        'unit': 'ml'
    },
    2: {
        'measurement': 'duration_time',
        'unit': 's'
    },
    3: {
        'measurement': 'volume',
        'unit': 'ml'
    }
}

channels_dict = {
    0: {
        'name': 'Motor A',
        'types': ['volume', 'on_off'],
        'measurements': [0, 1]
    },
    1: {
        'name': 'Motor B',
        'types': ['volume', 'on_off'],
        'measurements': [2, 3]
    }
}

# Output information
OUTPUT_INFORMATION = {
    'output_name_unique': 'GROVE_I2C_MOTOR',
    'output_name': "{}: I2C_MOTOR".format(
        lazy_gettext("DC Motor Controller")),
    'output_library': 'smbus2',
    'measurements_dict': measurements_dict,
    'channels_dict': channels_dict,
    'output_types': ['volume', 'on_off'],

    'url_additional': 'http://junkroom2cyberrobotics.blogspot.com/2013/02/raspberry-pi-grove-i2c-motor-driver.html',

    'message': 'The Grover I2C motor controller can control 2 DC motors. If these motors control peristaltic pumps, set the Flow Rate '
               'and the output can can be instructed to dispense volumes in addition to being turned on dor durations.',

    'options_enabled': [
	'i2c_location', 
        'button_on',
        'button_send_duration',
        'button_send_volume'
    ],
    'options_disabled': ['interface'],

    'dependencies_module': [
        ('pip-pypi', 'smbus2', 'smbus2==0.4.1') 
    ],

    'interfaces': ['I2C'], 
    'i2c_location': [
        '0x28'
    ],
    'i2c_address_editable': True,
    'i2c_address_default': '0x28',

    'custom_channel_options': [
        {
            'id': 'name',
            'type': 'text',
            'default_value': '',
            'required': False,
            'name': lazy_gettext('Name'),
            'phrase': lazy_gettext('A name for this motor')
        },
        {  
            'id': 'motor_speed',
            'type': 'integer',
            'default_value': 50,
            'constraints_pass': constraints_pass_percent,
            'name': 'Speed',
            'phrase': 'The speed of the motor (value, 0 - 100%)'
        },
        {
            'id': 'direction',
            'type': 'select',
            'default_value': 1,
            'options_select': [
                (1, 'Forward'),
                (0, 'Backward')
            ],
            'name': lazy_gettext('Direction'),
            'phrase': 'The direction to turn the motor'
        },
        {
            'id': 'flow_rate_ml_min',
            'type': 'float',
            'default_value': 150.0,
            'constraints_pass': constraints_pass_positive_value,
            'name': 'Volume Rate (ml/min)',
            'phrase': 'If a pump, the measured flow rate (ml/min) at the set Duty Cycle'
        },
    ]
}


class OutputModule(AbstractOutput):
    """
    An output support class that operates an output
    """
    def __init__(self, output, testing=False):
        super(OutputModule, self).__init__(output, testing=testing, name=__name__)

        self.driver = None
        self.currently_dispensing = False
        self.output_setup = False
        self.channel_setup = {}
        self.gpio = None

        output_channels = db_retrieve_table_daemon(
            OutputChannel).filter(OutputChannel.output_id == self.output.unique_id).all()
        self.options_channels = self.setup_custom_channel_options_json(
            OUTPUT_INFORMATION['custom_channel_options'], output_channels)

    def setup_output(self):
        self.setup_output_variables(OUTPUT_INFORMATION)

        import RPi.GPIO as GPIO
        self.gpio = GPIO

        for channel in channels_dict:
            if (not self.options_channels['pin_1'][channel] or
                    not self.options_channels['pin_2'][channel] or
                    not self.options_channels['pin_enable'][channel] or
                    not self.options_channels['duty_cycle'][channel]):
                self.logger.error("Cannot initialize Output channel {} until all options are set. "
                                  "Check your configuration.".format(channel))
                self.channel_setup[channel] = False
            else:
                self.gpio.setmode(self.gpio.BCM)
                self.gpio.setup(self.options_channels['pin_1'][channel], self.gpio.OUT)
                self.gpio.setup(self.options_channels['pin_2'][channel], self.gpio.OUT)
                self.gpio.setup(self.options_channels['pin_enable'][channel], self.gpio.OUT)
                self.stop(channel)
                self.driver = self.gpio.PWM(self.options_channels['pin_enable'][channel], 1000)
                self.driver.start(self.options_channels['duty_cycle'][channel])
                self.channel_setup[channel] = True
                self.output_setup = True
                self.output_states[channel] = False

    def output_switch(self, state, output_type=None, amount=None, output_channel=None):
        if not self.channel_setup[output_channel]:
            msg = "Output channel {} not set up, cannot turn it on or off.".format(output_channel)
            self.logger.error(msg)
            return msg

        if amount is not None and amount < 0:
            self.logger.error("Amount cannot be less than 0")
            return

        if state == 'on' and output_type == 'vol' and amount:
            if self.currently_dispensing:
                self.logger.debug("DC motor instructed to dispense volume while it's already dispensing a volume. "
                                  "Overriding current dispense with new instruction.")

            total_dispense_seconds = amount / self.options_channels['flow_rate_ml_min'][0] * 60
            msg = "Turning pump on for {sec:.1f} seconds to dispense {ml:.1f} ml (at {rate:.1f} ml/min).".format(
                sec=total_dispense_seconds,
                ml=amount,
                rate=self.options_channels['flow_rate_ml_min'][0])
            self.logger.debug(msg)

            write_db = threading.Thread(
                target=self.dispense_volume,
                args=(output_channel, amount, total_dispense_seconds,))
            write_db.start()
            return
        if state == 'on' and output_type == 'sec':
            if self.currently_dispensing:
                self.logger.debug(
                    "DC motor instructed to turn on while it's already dispensing a volume. "
                    "Overriding current dispense with new instruction.")
            self.run(output_channel)
        elif state == 'off':
            if self.currently_dispensing:
                self.currently_dispensing = False
            self.stop(output_channel)

    def dispense_volume(self, channel, amount, total_dispense_seconds):
        """ Dispense at flow rate """
        self.currently_dispensing = True
        self.logger.debug("Output turned on")
        self.run(channel)
        timer_dispense = time.time() + total_dispense_seconds

        while time.time() < timer_dispense and self.currently_dispensing:
            time.sleep(0.01)

        self.stop(channel)
        self.currently_dispensing = False
        self.logger.debug("Output turned off")
        self.record_dispersal(channel, amount, total_dispense_seconds)

    def record_dispersal(self, channel, amount, total_on_seconds):
        measure_dict = copy.deepcopy(measurements_dict)
        measure = channels_dict[channel]['measurements']
        measure_dict[measure[0]]['value'] = total_on_seconds
        measure_dict[measure[1]]['value'] = amount
        add_measurements_influxdb(self.unique_id, measure_dict)

    """ Change the stuff below to shoot out I2C commands """
    def run(self, channel):
        if self.options_channels['direction'][channel]:
            self.gpio.output(self.options_channels['pin_1'][channel], self.gpio.HIGH)
            self.gpio.output(self.options_channels['pin_2'][channel], self.gpio.LOW)
            self.output_states[channel] = True
        else:
            self.gpio.output(self.options_channels['pin_1'][channel], self.gpio.LOW)
            self.gpio.output(self.options_channels['pin_2'][channel], self.gpio.HIGH)
            self.output_states[channel] = True

    def stop(self, channel):
        self.gpio.output(self.options_channels['pin_1'][channel], self.gpio.LOW)
        self.gpio.output(self.options_channels['pin_2'][channel], self.gpio.LOW)
        self.output_states[channel] = False

    def is_on(self, output_channel=None):
        if self.is_setup(channel=output_channel):
            return self.output_states[output_channel]

    def is_setup(self, channel=None):
        if channel:
            return self.channel_setup[channel]
        if True in self.channel_setup.values():
            return True

    def stop_output(self):
        """ Called when Output is stopped """
        for channel in channels_dict:
            self.stop(channel)
        self.running = False

        """
        Grove I2C MotorDriver register definition.
        #define SET_PWM_AB (0x82)
        #define SET_FREQ (0x84)
        #define CHG_ADDR (0x83)
        #define CHANEL_SET (0xaa)
        #define MOTOR1_SPD (0xa1)
        #define MOTOR2_SPD (0xa5)
        """