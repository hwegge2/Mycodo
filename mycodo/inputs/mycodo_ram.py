# coding=utf-8
import logging
import resource

from mycodo.mycodo_client import DaemonControl
from .base_input import AbstractInput

logger = logging.getLogger("mycodo.inputs.mycodo_ram")


class MycodoRam(AbstractInput):
    """
    A sensor support class that measures ram used by the Mycodo daemon

    """
    def __init__(self, testing=False):
        super(MycodoRam, self).__init__()
        self._disk_space = None

        if not testing:
            self.control = DaemonControl()

    def __repr__(self):
        """  Representation of object """
        return "<{cls}(disk_space={disk_space})>".format(
                cls=type(self).__name__,
                disk_space="{0:.2f}".format(self._disk_space))

    def __str__(self):
        """ Return measurement information """
        return "Ram: {disk_space}".format(
                disk_space="{0:.2f}".format(self._disk_space))

    def __iter__(self):  # must return an iterator
        """ SensorClass iterates through live measurement readings """
        return self

    def next(self):
        """ Get next measurement reading """
        if self.read():  # raised an error
            raise StopIteration  # required
        return dict(disk_space=float('{0:.2f}'.format(self._disk_space)))

    @property
    def disk_space(self):
        """ Mycodo daemon disk_space in MegaBytes """
        if self._disk_space is None:  # update if needed
            self.read()
        return self._disk_space

    def get_measurement(self):
        """ Gets the measurement in units by reading resource """
        self._disk_space = None
        disk_space = None
        try:
            disk_space = resource.getrusage(
                resource.RUSAGE_SELF).ru_maxrss / float(1000)
        except Exception:
            pass
        return disk_space

    def read(self):
        """
        Takes a ram usage reading from resource and updates the self._disk_space

        :returns: None on success or 1 on error
        """
        try:
            self._disk_space = self.get_measurement()
            if self._disk_space is not None:
                return  # success - no errors
        except Exception as e:
            logger.exception(
                "{cls} raised an exception when taking a reading: "
                "{err}".format(cls=type(self).__name__, err=e))
        return 1
