# Copyright (c) 2017 Ultimaker B.V.
# Cura is released under the terms of the LGPLv3 or higher.
from typing import TYPE_CHECKING

from cura.PrinterOutput.PrinterOutputController import PrinterOutputController

if TYPE_CHECKING:
    from cura.PrinterOutput.PrintJobOutputModel import PrintJobOutputModel
    from cura.PrinterOutputDevice import PrinterOutputDevice


class ClusterUM3PrinterOutputController(PrinterOutputController):
    
    def __init__(self, output_device: "PrinterOutputDevice") -> None:
        super().__init__(output_device)
        self.can_pre_heat_bed = False
        self.can_pre_heat_hotends = False
        self.can_control_manually = False
        self.can_send_raw_gcode = False

    def setJobState(self, job: "PrintJobOutputModel", state: str) -> None:
        data = "{\"action\": \"%s\"}" % state
        self._output_device.put("print_jobs/%s/action" % job.key, data, on_finished = None)
