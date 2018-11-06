# Copyright (c) 2017 Ultimaker B.V.
# Cura is released under the terms of the LGPLv3 or higher.
import json
from typing import TYPE_CHECKING, Optional, Dict, Callable

from zeroconf import Zeroconf, ServiceBrowser, ServiceStateChange, ServiceInfo
from queue import Queue
from threading import Event, Thread
from time import time
from PyQt5.QtNetwork import QNetworkRequest, QNetworkAccessManager, QNetworkReply
from PyQt5.QtCore import QUrl

from UM.OutputDevice.OutputDevicePlugin import OutputDevicePlugin
from UM.Logger import Logger
from UM.Application import Application
from UM.Signal import Signal, signalemitter
from UM.Version import Version
from cura.API import Account

from . import ClusterUM3OutputDevice, LegacyUM3OutputDevice


if TYPE_CHECKING:
    from cura.CuraApplication import CuraApplication


## This plugin handles the connection detection & creation of output device objects for the UM3 printer.
#  Zero-Conf is used to detect printers, which are saved in a dict.
#  If we discover a printer that has the same key as the active machine instance a connection is made.
@signalemitter
class UM3OutputDevicePlugin(OutputDevicePlugin):
    
    addDeviceSignal = Signal()
    removeDeviceSignal = Signal()
    discoveredDevicesChanged = Signal()

    def __init__(self, application: "CuraApplication"):
        super().__init__()
        self._application = application  # type: CuraApplication

        # Ultimaker device type registry. Type registry number is returned by the system API.
        self._printer_type_identifiers = {
            "9066": "ultimaker3",
            "9511": "ultimaker3_extended",
            "9051": "ultimaker_s5"
        }
        
        # Networking instances.
        self._network_manager = QNetworkAccessManager()
        self._network_manager.finished.connect(self._onNetworkRequestFinished)

        # API strategies.
        self._min_cluster_version = Version("4.0.0")
        self._min_cloud_version = Version("5.2.0")
        
        # Printer HTTP API.
        self._local_api_version = "1"
        self._local_api_root = "api/v{}".format(self._local_api_version)
        self._local_api_system = "{}/system".format(self._local_api_root)
        
        # Cluster HTTP API.
        self._local_cluster_api_version = "1"
        self._local_cluster_api_root = "cluster-api/v{}".format(self._local_cluster_api_version)
        self._local_cluster_api_printers = "{}/printers".format(self._local_cluster_api_root)
        
        # Cloud HTTP API.
        self._cloud_api_version = "1"
        self._cloud_api_root = "https://api.ultimaker.com/connect/v{}".format(self._cloud_api_version)
        self._cloud_api_clusters = "{}/clusters/".format(self._cloud_api_root)

        # Get list of manual instances from preferences
        # The format is a comma-separated list of IP addresses and/or host names.
        self._manual_instances_preference_key = "um3networkprinting/manual_instances"
        self._application.getPreferences().addPreference(self._manual_instances_preference_key, "")
        self._manual_instances = self._preferences.getValue(self._manual_instances_preference_key).split(",")

        # Store the last manual entry key
        self._last_manual_entry_key = ""  # type: str

        # ZeroConf/discovery instances.
        self._zero_conf = None  # type: Optional[Zeroconf]
        self._zero_conf_browser = None
        self._discovered_devices = { }
        
        # The zero-conf service changed requests are handled in a separate thread,
        # so we can re-schedule the requests which fail to get detailed service info.
        # Any new or re-scheduled requests will be appended to the request queue,
        # and the handling thread will pick them up and process them.
        self._service_changed_request_queue = Queue()
        self._service_changed_request_event = Event()
        self._service_changed_request_thread = Thread(target = self._handleOnServiceChangedRequests, daemon = True)
        self._service_changed_request_thread.start()

        # Because the model needs to be created in the same thread as the QMLEngine, we use a signal.
        self.addDeviceSignal.connect(self._onAddDevice)
        self.removeDeviceSignal.connect(self._onRemoveDevice)
        
        # Check all connections when switching active machine.
        application.globalContainerStackChanged.connect(self.reCheckConnections)

    def getDiscoveredDevices(self):
        return self._discovered_devices

    def getLastManualDevice(self) -> str:
        return self._last_manual_entry_key

    def resetLastManualDevice(self) -> None:
        self._last_manual_entry_key = ""

    ##  Start looking for devices on the network.
    def start(self):
        self.startDiscovery()

    ##  Start looking for devices on the network.
    def startDiscovery(self):
        self.stop()
        if self._zero_conf_browser:
            self._zero_conf_browser.cancel()
            self._zero_conf_browser = None  # Force the old ServiceBrowser to be destroyed.

        for instance_name in list(self._discovered_devices):
            self._onRemoveDevice(instance_name)

        self._zero_conf = Zeroconf()
        self._zero_conf_browser = ServiceBrowser(self._zero_conf, u'_ultimaker._tcp.local.',
                                                 [self._appendServiceChangedRequest])

        # Look for manual instances from preference
        for address in self._manual_instances:
            if address:
                self.addManualDevice(address)
        self.resetLastManualDevice()

    ##  Check all connections.
    def reCheckConnections(self):
        active_machine = self._application.getGlobalContainerStack()
        if not active_machine:
            return

        um_network_key = active_machine.getMetaDataEntry("um_network_key")

        for key in self._discovered_devices:
            if key == um_network_key:
                if not self._discovered_devices[key].isConnected():
                    Logger.log("d", "Attempting to connect with [%s]" % key)
                    self._discovered_devices[key].connect()
                    self._discovered_devices[key].connectionStateChanged.connect(self._onDeviceConnectionStateChanged)
                else:
                    self._onDeviceConnectionStateChanged(key)
            else:
                if self._discovered_devices[key].isConnected():
                    Logger.log("d", "Attempting to close connection with [%s]" % key)
                    self._discovered_devices[key].close()
                    self._discovered_devices[key].connectionStateChanged.disconnect(self._onDeviceConnectionStateChanged)

    ##  Callback for when a device connection state changed to either connected or disconnected.
    def _onDeviceConnectionStateChanged(self, key) -> None:
    
        # If we don't care about the device we ignore the status change.
        if key not in self._discovered_devices:
            return

        # If it disconnected we remove the device from the manager.
        if not self._discovered_devices[key].isConnected():
            self.getOutputDeviceManager().removeOutputDevice(key)
            return

        # If the activated global machine changed before the connection state did, we ignore the status change.
        um_network_key = self._application.getGlobalContainerStack().getMetaDataEntry("um_network_key")
        if not um_network_key or key != um_network_key:
            self.getOutputDeviceManager().addOutputDevice(self._discovered_devices[key])

    ##  Stop checking for devices on the network.
    def stop(self):
        if not self._zero_conf:
            return
        Logger.log("d", "Closing ZeroConf...")
        self._zero_conf.close()

    ##  Remove a networked device.
    def removeManualDevice(self, key: str, address: str = None):
        if key in self._discovered_devices:
            if not address:
                address = self._discovered_devices[key].ipAddress
            self._onRemoveDevice(key)
            self.resetLastManualDevice()

        if address in self._manual_instances:
            self._manual_instances.remove(address)
            self._preferences.setValue(self._manual_instances_preference_key, ",".join(self._manual_instances))

    ##  Manually add a networked device to check and connect to.
    def addManualDevice(self, address: str):
        if address not in self._manual_instances:
            self._manual_instances.append(address)
            self._preferences.setValue(self._manual_instances_preference_key, ",".join(self._manual_instances))

        instance_name = "manual:%s" % address
        properties = {
            b"name": address.encode("utf-8"),
            b"address": address.encode("utf-8"),
            b"manual": b"true",
            b"incomplete": b"true",
            b"temporary": b"true"   # Still a temporary device until all the info is retrieved in _onNetworkRequestFinished
        }

        if instance_name not in self._discovered_devices:
            # Add a preliminary printer instance
            self._onAddDevice(instance_name, address, properties)
            
        self._last_manual_entry_key = instance_name
        self._checkManualDevice(address)
        
    ##  If a user is signed in, we ask the Ultimaker Cloud API for a list of their connected clusters.
    #   Each cluster also returns the local IP address so we can cross-reference the local and remove clusters later on.
    def _checkCloudDevices(self) -> None:
        ultimaker_account = self._application.getCuraAPI().account  # type: Account
        
        if not ultimaker_account.isLoggedIn:
            # The user is not logged in so we cannot get the remote clusters.
            return
        
        url = QUrl(self._cloud_api_clusters)
        request = QNetworkRequest(url)
        self._network_manager.get(request)
        
    def _onCheckCloudDevices(self, reply: QNetworkReply) -> None:
        pass

    ##  Check if a UM3 family device exists at this address.
    #   If a printer responds, it will replace the preliminary printer created above
    #   origin=manual is for tracking back the origin of the call
    def _checkManualDevice(self, address: str) -> None:
        url = QUrl("http://{}/{}".format(address, self._local_api_system))
        request = QNetworkRequest(url)
        self._network_manager.get(request)

    ##  Handle the response of a manual device check.
    #   This call was initiated by self._checkManualDevice
    def _onCheckManualDevice(self, reply: QNetworkReply) -> None:
        if reply.attribute(QNetworkRequest.HttpStatusCodeAttribute) != 200:
            # Something went wrong with checking the firmware version!
            return
    
        try:
            system_info = json.loads(bytes(reply.readAll()).decode("utf-8"))
        except:
            Logger.log("e", "Something went wrong converting the JSON.")
            return
    
        address = reply.url().host()
        has_cluster_capable_firmware = Version(system_info["firmware"]) > self._min_cluster_version
        has_cloud_capable_firmware = Version(system_info["firmware"] > self._min_cloud_version)
        
        instance_name = "manual:%s" % address
        properties = {
            b"name": (system_info["name"] + " (manual)").encode("utf-8"),
            b"address": address.encode("utf-8"),
            b"firmware_version": system_info["firmware"].encode("utf-8"),
            b"manual": b"true",
            b"machine": str(system_info['hardware']["typeid"]).encode("utf-8")
        }
    
        if has_cluster_capable_firmware:
            # Cluster needs an additional request, before it's completed.
            properties[b"incomplete"] = b"true"
    
        # Check if the device is still in the list & re-add it with the updated
        # information.
        if instance_name in self._discovered_devices:
            self._onRemoveDevice(instance_name)
            self._onAddDevice(instance_name, address, properties)
    
        if has_cluster_capable_firmware:
            # We need to request more info in order to figure out the size of the cluster.
            self._checkClusterPrinters(address)
            
        if has_cluster_capable_firmware and has_cloud_capable_firmware:
            # We need to request the cloud info to figure out which clusters are available remotely.
            self._checkCloudDevices()
            
    ##  Get more details about the printers in a cluster.
    def _checkClusterPrinters(self, address: str):
        url = QUrl("http://{}/{}".format(address, self._local_cluster_api_printers))
        request = QNetworkRequest(url)
        self._network_manager.get(request)
    
    ##  Handle the response of getting the details about all printers in a cluster.
    #   This call was initiated by self._checkClusterPrinters
    def _onCheckClusterPrinters(self, reply: QNetworkReply):
        if reply.attribute(QNetworkRequest.HttpStatusCodeAttribute) != 200:
            # Something went wrong with checking the amount of printers the cluster has!
            return
        # So we confirmed that the device is in fact a cluster printer, and we should now know how big it is.
        try:
            cluster_printers_list = json.loads(bytes(reply.readAll()).decode("utf-8"))
        except:
            Logger.log("e", "Something went wrong converting the JSON.")
            return
        address = reply.url().host()
        instance_name = "manual:%s" % address
        if instance_name in self._discovered_devices:
            device = self._discovered_devices[instance_name]
            properties = device.getProperties().copy()
            if b"incomplete" in properties:
                del properties[b"incomplete"]
            properties[b'cluster_size'] = len(cluster_printers_list)
            self._onRemoveDevice(instance_name)
            self._onAddDevice(instance_name, address, properties)

    ##  Handle network request replies from the network manager.
    #   Automatically maps the request URL to the correct callback for further handling.
    def _onNetworkRequestFinished(self, reply):
        reply_url_to_callback_map = {
            self._cloud_api_clusters: self._onCheckCloudDevices,
            self._local_api_system: self._onCheckManualDevice,
            self._local_cluster_api_printers: self._onCheckClusterPrinters
        }  # type: Dict[str, Callable]
        
        # Find the appropriate callback URL to handle this reply.
        # We simply call the first one that matches the request URL signature.
        for url, callback in reply_url_to_callback_map:
            if url in reply.url().toString():
                return callback(reply)
        
        # For some reason no callback method was found, so we should at least log that this happened.
        Logger.log("w", "No callback method was selected for a network reply: %s", reply.url().toString())

    def _onRemoveDevice(self, device_id):
        device = self._discovered_devices.pop(device_id, None)
        if device:
            if device.isConnected():
                device.disconnect()
                try:
                    device.connectionStateChanged.disconnect(self._onDeviceConnectionStateChanged)
                except TypeError:
                    # Disconnect already happened.
                    pass

            self.discoveredDevicesChanged.emit()

    ##  Check what kind of device we need to add.
    #   Depending on the firmware we either add a "Connect"/"Cluster" or "Legacy" UM3 device.
    def _onAddDevice(self, name: str, address: str, properties):
        cluster_size = int(properties.get(b"cluster_size", -1))
        printer_type = properties.get(b"machine", b"").decode("utf-8")

        for key, value in self._printer_type_identifiers.items():
            if printer_type.startswith(key):
                properties[b"printer_type"] = bytes(value, encoding="utf8")
                break
        else:
            properties[b"printer_type"] = b"Unknown"
            
        if cluster_size >= 0:
            device = ClusterUM3OutputDevice.ClusterUM3OutputDevice(name, address, properties)
        else:
            device = LegacyUM3OutputDevice.LegacyUM3OutputDevice(name, address, properties)

        self._discovered_devices[device.getId()] = device
        self.discoveredDevicesChanged.emit()

        global_container_stack = self._application.getGlobalContainerStack()
        if global_container_stack and device.getId() == global_container_stack.getMetaDataEntry("um_network_key"):
            device.connect()
            device.connectionStateChanged.connect(self._onDeviceConnectionStateChanged)

    ##  Appends a service changed request so later the handling thread will pick it up and processes it.
    def _appendServiceChangedRequest(self, zeroconf, service_type, name, state_change):
        # append the request and set the event so the event handling thread can pick it up
        item = (zeroconf, service_type, name, state_change)
        self._service_changed_request_queue.put(item)
        self._service_changed_request_event.set()

    def _handleOnServiceChangedRequests(self):
        while True:
            # Wait for the event to be set
            self._service_changed_request_event.wait(timeout = 5.0)

            # Stop if the application is shutting down
            if self._application.isShuttingDown():
                return

            self._service_changed_request_event.clear()

            # Handle all pending requests
            reschedule_requests = []  # A list of requests that have failed so later they will get re-scheduled
            while not self._service_changed_request_queue.empty():
                request = self._service_changed_request_queue.get()
                zeroconf, service_type, name, state_change = request
                try:
                    result = self._onServiceChanged(zeroconf, service_type, name, state_change)
                    if not result:
                        reschedule_requests.append(request)
                except Exception:
                    Logger.logException("e", "Failed to get service info for [%s] [%s], the request will be rescheduled",
                                        service_type, name)
                    reschedule_requests.append(request)

            # Re-schedule the failed requests if any
            if reschedule_requests:
                for request in reschedule_requests:
                    self._service_changed_request_queue.put(request)

    ##  Handler for zeroConf detection.
    #   Return True or False indicating if the process succeeded.
    #   Note that this function can take over 3 seconds to complete. Be carefull calling it from the main thread.
    def _onServiceChanged(self, zero_conf, service_type, name, state_change):
        if state_change == ServiceStateChange.Added:
            Logger.log("d", "Bonjour service added: %s" % name)

            # First try getting info from zero-conf cache
            info = ServiceInfo(service_type, name, properties={})
            for record in zero_conf.cache.entries_with_name(name.lower()):
                info.update_record(zero_conf, time(), record)

            for record in zero_conf.cache.entries_with_name(info.server):
                info.update_record(zero_conf, time(), record)
                if info.address:
                    break

            # Request more data if info is not complete
            if not info.address:
                Logger.log("d", "Trying to get address of %s", name)
                info = zero_conf.get_service_info(service_type, name)

            if info:
                type_of_device = info.properties.get(b"type", None)
                if type_of_device:
                    if type_of_device == b"printer":
                        address = '.'.join(map(lambda n: str(n), info.address))
                        self.addDeviceSignal.emit(str(name), address, info.properties)
                    else:
                        Logger.log("w",
                                   "The type of the found device is '%s', not 'printer'! Ignoring.." % type_of_device)
            else:
                Logger.log("w", "Could not get information about %s" % name)
                return False

        elif state_change == ServiceStateChange.Removed:
            Logger.log("d", "Bonjour service removed: %s" % name)
            self.removeDeviceSignal.emit(str(name))

        return True