#!/usr/bin/env python3

"""MicroCloud charm."""

import json
import logging
import os
import shutil
import subprocess
import tarfile
import tempfile
import time
from typing import Dict, List, Union

from charms.operator_libs_linux.v2.snap import SnapCache, SnapError, SnapState
from charms.operator_libs_linux.v2.snap import install_local as snap_install_local
from ops.charm import (
    CharmBase,
    ConfigChangedEvent,
    InstallEvent,
    RelationCreatedEvent,
    RelationJoinedEvent,
    StartEvent,
    StopEvent,
    UpdateStatusEvent,
)
from ops.framework import StoredState
from ops.main import main
from ops.model import (
    ActiveStatus,
    BlockedStatus,
    MaintenanceStatus,
    ModelError,
    WaitingStatus,
)

logger = logging.getLogger(__name__)


class MaasMicroCloudCharm(CharmBase):
    """MicroCloud charm class."""

    _stored = StoredState()

    def __init__(self, *args):
        """Initialize charm's variable."""
        super().__init__(*args)

        # Initialize the persistent storage if needed
        self._stored.set_default(
            config={},
            microcloud_binary_path="",
            microcloud_snap_path="",
        )

        # Main event handlers
        self.framework.observe(self.on.install, self._on_charm_install)
        self.framework.observe(self.on.config_changed, self._on_charm_config_changed)
        self.framework.observe(self.on.start, self._on_charm_start)
        self.framework.observe(self.on.update_status, self._on_update_status)
        self.framework.observe(self.on.stop, self._on_charm_stop)

        # Relation event handlers
        self.framework.observe(self.on.cluster_relation_created, self._on_cluster_relation_created)
        self.framework.observe(self.on.cluster_relation_joined, self._on_cluster_relation_joined)

    @property
    def peers(self):
        """Fetch the cluster relation."""
        return self.model.get_relation("cluster")

    def get_peer_data_str(self, bag, key: str) -> str:
        """Retrieve a str from the peer data bag."""
        if not self.peers or not bag or not key:
            return ""

        value = self.peers.data[bag].get(key, "")
        if isinstance(value, str):
            return value

        logger.error(f"Invalid data pulled out from {bag.name}.get('{key}')")
        return ""

    def set_peer_data_str(self, bag, key: str, value: str) -> None:
        """Put a str into the peer data bag if not there or different."""
        if not self.peers or not bag or not key:
            return

        old_value: str = self.get_peer_data_str(bag, key)
        if old_value != value:
            self.peers.data[bag][key] = value

    def _on_charm_install(self, event: InstallEvent) -> None:
        logger.info("Installing the MicroCloud charm")
        # Confirm that the config is valid
        if not self.config_is_valid():
            return

        # Install MicroCloud itself
        try:
            self.snap_install_microcloud()
            logger.info("Microcloud installed successfully")
        except RuntimeError:
            logger.error("Failed to install MicroCloud")
            event.defer()
            return

        # Apply side-loaded resources attached at deploy time
        self.resource_sideload()

    def _on_cluster_relation_created(self, event: RelationCreatedEvent) -> None:
        """We must wait for all units to be ready before initializing MicroCloud."""
        self.set_peer_data_str(self.unit, "clustered", "False")
        return

    def _on_charm_start(self, event: StartEvent) -> None:
        logger.info("Starting the MicroCloud charm")

        if self.config_changed():
            logger.debug("Pending config changes detected")
            self._on_charm_config_changed(event)

        one_unit_clustered = False
        for unit in self.peers.units:
            if self.peers.data[unit].get("clustered") == "True":
                one_unit_clustered = True
                break

        if one_unit_clustered:
            # check if this unit has been clustered by the init process
            try:
                subprocess.run(
                    ["lxc", "cluster", "list"],
                    check=True,
                    timeout=600,
                )
                self.set_peer_data_str(self.unit, "clustered", "True")
                self.unit_active("Healthy MicroCloud unit")
                return
            except subprocess.CalledProcessError:
                self.unit_waiting("This unit has not joined the cluster yet")
                event.defer()
                return
            except subprocess.TimeoutExpired:
                self.unit_blocked("This unit timed out checking its clustered status")
                return

        new_peers = [
            self.peers.data[unit].get("clustered") == "False" for unit in self.peers.units
        ]
        if (
            self.unit.is_leader()
            and self.get_peer_data_str(self.unit, "clustered") == "False"
            and all(new_peers)
            and self.app.planned_units() == len(self.peers.units) + 1
        ):
            try:
                self.microcloud_init()
                self.set_peer_data_str(
                    self.unit, "clustered", "True"
                )  # This unit is sure to be clustered
                # TODO: we can't say for sure that the cluster contains self.app.planned_units()
                # some nodes might have failed to join the cluster but the command result
                # is still a code 0. A workaround would be to parse the number of lines
                # of `lxc cluster list -f csv` on this node.
                self.unit_active("MicroCloud successfully initialized")
                return
            except RuntimeError as e:
                logger.error(f"Failed to initialize MicroCloud: {e}")
                self.unit_blocked("Failed to initialize MicroCloud")
                return

        time.sleep(10)  # Wait a bit before deferring the event

        if self.unit.is_leader():
            self.unit_waiting("Leader needs to wait for all units to be ready to bootstrap")
        else:
            self.unit_waiting("Unit needs to wait for all units to be ready to bootstrap")

        event.defer()
        return

    def _on_update_status(self, event: UpdateStatusEvent) -> None:
        """Regularly check if the unit is clustered."""
        try:
            subprocess.run(
                ["lxc", "cluster", "list"],
                check=True,
                timeout=600,
            )
            self.set_peer_data_str(self.unit, "clustered", "True")
            self.unit_active("Healthy MicroCloud unit")
        except subprocess.CalledProcessError:
            self.unit_blocked("This unit has failed to join the cluster")
            return
        except subprocess.TimeoutExpired:
            self.unit_blocked("This unit timed out checking its clustered status")
            return

    def _on_charm_config_changed(self, event: Union[ConfigChangedEvent, StartEvent]) -> None:
        """React to configuration changes. (JuJu refresh)."""
        logger.info("Updating charm config")

        # Confirm that the config is valid
        if not self.config_is_valid():
            return

        # Get all the configs that changed
        changed = self.config_changed()
        if not changed:
            logger.debug("No configuration changes to apply")
            return
        else:
            if "microceph" in changed or "microovn" in changed:
                logger.warning(
                    "MicroCeph and MicroOVN can only be enabled / disabled at deploy time. Ignoring the changes"
                )

        # Apply all the configs that changed
        try:
            if (
                "snap-channel-lxd" in changed
                or "snap-channel-microcloud" in changed
                or "snap-channel-microceph" in changed
                or "snap-channel-microovn" in changed
            ):
                logger.info("Changes have been detected in the snap channels, updating the snaps")
                self.snap_install_microcloud()
        except RuntimeError:
            msg = "Failed to apply some configuration change(s): %s" % ", ".join(changed)
            self.unit_blocked(msg)
            event.defer()
            return

    def _on_cluster_relation_joined(self, event: RelationJoinedEvent) -> None:
        """Add a new node to the existing MicroCloud cluster."""
        if (
            self.unit.is_leader()
            and self.get_peer_data_str(self.unit, "clustered") == "True"
            and event.unit
            != self.unit  # Don't add the leader to the cluster as it is already there
        ):
            try:
                self.microcloud_add()
                logger.info("New MicroCloud node successfully added")
                return
            except RuntimeError:
                logger.error("Failed to add a new MicroCloud node")
                return

    def _on_charm_stop(self, event: StopEvent) -> None:
        """Effectively remove this node from the existing MicroCloud cluster."""
        if self.get_peer_data_str(self.unit, "clustered") == "True":
            try:
                self.microcloud_remove(os.uname().nodename)
                logger.info("MicroCloud node successfully removed")
            except RuntimeError:
                logger.error("Failed to remove a MicroCloud node, retrying later")

    def config_changed(self) -> Dict:
        """Figure out what changed."""
        new_config = self.config
        old_config = self._stored.config
        apply_config = {}
        for k, v in new_config.items():
            if k not in old_config:
                apply_config[k] = v
            elif v != old_config[k]:
                apply_config[k] = v

        return apply_config

    def config_is_valid(self) -> bool:
        """Validate the config."""
        config_changed = self.config_changed()
        logger.info(f"Validating config: {config_changed}")
        return True

    def microcloud_init(self) -> None:
        """Apply initial configuration of MicroCloud."""
        self.unit_maintenance("Initializing MicroCloud")

        try:
            microcloud_process_init = subprocess.run(
                ["microcloud", "init", "--auto"],
                capture_output=True,
                check=True,
                timeout=600,
                text=True,
            )

            subprocess.run(
                ["microceph", "enable", "rgw"],
                check=True,
                timeout=600,
            )

            logger.info(f"MicroCloud successfully initialized:\n{microcloud_process_init.stdout}")
        except subprocess.CalledProcessError as e:
            self.unit_blocked(f'Failed to run "{e.cmd}": {e.stderr} ({e.returncode})')
            raise RuntimeError
        except subprocess.TimeoutExpired as e:
            self.unit_blocked(f'Timeout exceeded while running "{e.cmd}"')
            raise RuntimeError

    def microcloud_add(self) -> None:
        """Add a new node to MicroCloud."""
        self.unit_maintenance("Adding node to MicroCloud")

        try:
            microcloud_process_add = subprocess.run(
                ["microcloud", "add", "--auto"],
                capture_output=True,
                check=True,
                timeout=600,
                text=True,
            )
            logger.info(f"MicroCloud node(s) successfully added:\n{microcloud_process_add.stdout}")
        except subprocess.CalledProcessError as e:
            self.unit_blocked(f'Failed to run "{e.cmd}": {e.stderr} ({e.returncode})')
            raise RuntimeError
        except subprocess.TimeoutExpired as e:
            self.unit_blocked(f'Timeout exceeded while running "{e.cmd}"')
            raise RuntimeError

    def microcloud_remove(self, node_name_to_remove: str) -> None:
        """Remove a node from MicroCloud."""
        try:
            # Check if instances are running on this node local storage
            result = subprocess.run(
                ["lxc", "list", "--all-projects", "--format=json"], capture_output=True, text=True
            )
            instances = json.loads(result.stdout)
            for inst in instances:
                if inst["location"] == node_name_to_remove:
                    self.unit_blocked(
                        "This Microcloud unit contains instances. You can't remove it."
                    )
                    return
        except subprocess.CalledProcessError as e:
            self.unit_blocked(
                f"Failed to remove {node_name_to_remove} from the MicroCloud cluster: {e}"
            )
            raise RuntimeError
        except subprocess.TimeoutExpired as e:
            self.unit_blocked(f'Timeout exceeded while running "{e.cmd}"')
            raise RuntimeError

        self.unit_maintenance("Removing node from MicroCloud")

        try:
            # Let's remove the node from the LXD cluster as well because
            # MicroCloud does not do it automatically for now.
            # see here https://github.com/canonical/microcloud/issues/160
            subprocess.run(
                ["lxc", "cluster", "remove", node_name_to_remove],
                capture_output=True,
                check=True,
                timeout=600,
                text=True,
            )

            logger.info(
                f"LXD cluster member successfully removed for the '{node_name_to_remove}' node"
            )

            # Same reason for MicroCeph: https://github.com/canonical/microcloud/issues/160
            if self._stored.config["snap-channel-microceph"]:
                subprocess.run(
                    ["microceph", "cluster", "remove", node_name_to_remove],
                    capture_output=True,
                    check=True,
                    timeout=600,
                    text=True,
                )

                logger.info(
                    f"MicroCeph cluster member successfully removed for the '{node_name_to_remove}' node"
                )

            # Same reason for microOVN: https://github.com/canonical/microcloud/issues/160
            if self._stored.config["snap-channel-microovn"]:
                subprocess.run(
                    ["microovn", "cluster", "remove", node_name_to_remove],
                    capture_output=True,
                    check=True,
                    timeout=600,
                    text=True,
                )

                logger.info(
                    f"MicroOVN cluster member successfully removed for the '{node_name_to_remove}' node"
                )

            # For now we don't check the status of ROLE. But in order to make it resilient,
            # we'd need to introduce a ROLE check to make sure that the node is not
            # in a PENDING state. see here
            # https://github.com/canonical/microcloud/issues/161
            subprocess.run(
                ["microcloud", "cluster", "remove", node_name_to_remove],
                capture_output=True,
                check=True,
                timeout=600,
                text=True,
            )

            logger.info(
                f"MicroCloud cluster member successfully removed for the '{node_name_to_remove}' node"
            )

        except subprocess.CalledProcessError as e:
            self.unit_blocked(f'Failed to run "{e.cmd}": {e.stderr} ({e.returncode})')
            raise RuntimeError
        except subprocess.TimeoutExpired as e:
            self.unit_blocked(f'Timeout exceeded while running "{e.cmd}"')
            raise RuntimeError

    def snap_install_microcloud(self) -> None:
        """Install MicroCloud from snap."""
        try:
            cache = SnapCache()
            cohort = "+"
            snapd = cache["snapd"]  # Always refresh snapd first
            snapd.ensure(SnapState.Latest, channel="stable")  # version 2.60.4

            microcloud = cache["microcloud"]
            if not microcloud.present:
                microcloud.ensure(
                    SnapState.Latest,
                    channel=self.config["snap-channel-microcloud"],
                    cohort=cohort,
                )

            microceph_enabled = self.config["microceph"]
            if microceph_enabled:
                microceph = cache["microceph"]
                if not microceph.present:
                    microceph.ensure(
                        SnapState.Latest,
                        channel=self.config["snap-channel-microceph"],
                        cohort=cohort,
                    )

                subprocess.run(
                    ["rm", "-rf", "/etc/ceph"],
                    check=True,
                    timeout=600,
                )
                subprocess.run(
                    ["ln", "-s", "/var/snap/microceph/current/conf/", "/etc/ceph"],
                    check=True,
                    timeout=600,
                )

            microovn_enabled = self.config["microovn"]
            if microovn_enabled:
                microovn = cache["microovn"]
                if not microovn.present:
                    microovn.ensure(
                        SnapState.Latest,
                        channel=self.config["snap-channel-microovn"],
                        cohort=cohort,
                    )

            lxd = cache["lxd"]
            if (
                not lxd.present
            ):  # This should already be installed but let's refresh it just in case
                lxd.ensure(
                    SnapState.Latest, channel=self.config["snap-channel-lxd"], cohort=cohort
                )
        except SnapError as e:
            logger.error(
                "An exception occurred when installing snap packages. Reason: %s", e.message
            )
            self.unit_blocked("An exception occurred when installing snap packages")
            raise RuntimeError

        # Done with the snap installation
        self._stored.config["snap-channel-lxd"] = self.config["snap-channel-lxd"]
        self._stored.config["snap-channel-microcloud"] = self.config["snap-channel-microcloud"]
        self._stored.config["snap-channel-microceph"] = self.config["snap-channel-microceph"]
        self._stored.config["snap-channel-microovn"] = self.config["snap-channel-microovn"]

    def microcloud_reload(self) -> None:
        """Reload the microcloud daemon."""
        self.unit_maintenance("Reloading MicroCloud")
        try:
            # Avoid occasional race during startup where a reload could cause a failure
            subprocess.run(
                ["microcloud", "waitready", "--timeout=30"], capture_output=True, check=False
            )

            cache = SnapCache()
            microcloud = cache["microcloud"]
            if microcloud.present:
                microcloud.restart(reload=True)

        except subprocess.CalledProcessError as e:
            self.unit_blocked(f'Failed to run "{e.cmd}": {e.stderr} ({e.returncode})')
            raise RuntimeError

    def resource_sideload(self) -> None:
        """Side-load resources."""
        # Multi-arch support
        arch: str = os.uname().machine
        possible_archs: List[str] = [arch]
        if arch == "x86_64":
            possible_archs = ["x86_64", "amd64"]

        # Microcloud snap
        microcloud_snap_resource: str = ""
        fname_suffix: str = ".snap"
        try:
            # Note: self._stored can only store simple data types (int/float/dict/list/etc)
            microcloud_snap_resource = str(self.model.resources.fetch("microcloud-snap"))
        except ModelError:
            pass

        tmp_dir: str = ""
        if microcloud_snap_resource and tarfile.is_tarfile(microcloud_snap_resource):
            logger.debug(f"{microcloud_snap_resource} is a tarball; unpacking")
            tmp_dir = tempfile.mkdtemp()
            tarball = tarfile.open(microcloud_snap_resource)
            valid_names = {f"microcloud_{x}{fname_suffix}" for x in possible_archs}
            for f in valid_names.intersection(tarball.getnames()):
                tarball.extract(f, path=tmp_dir)
                logger.debug(f"{f} was extracted from the tarball")
                self._stored.microcloud_snap_path = f"{tmp_dir}/{f}"
                break
            else:
                logger.debug("Missing arch specific snap from tarball")
            tarball.close()
        else:
            self._stored.microcloud_snap_path = microcloud_snap_resource

        if self._stored.microcloud_snap_path:
            self.snap_sideload_microcloud()
            if tmp_dir:
                os.remove(self._stored.microcloud_snap_path)
                os.rmdir(tmp_dir)

        # MicroCloud binary
        microcloud_binary_resource: str = ""
        fname_suffix = ""
        try:
            # Note: self._stored can only store simple data types (int/float/dict/list/etc)
            microcloud_binary_resource = str(self.model.resources.fetch("microcloud-binary"))
        except ModelError:
            pass

        tmp_dir = ""
        if microcloud_binary_resource and tarfile.is_tarfile(microcloud_binary_resource):
            logger.debug(f"{microcloud_binary_resource} is a tarball; unpacking")
            tmp_dir = tempfile.mkdtemp()
            tarball = tarfile.open(microcloud_binary_resource)
            valid_names = {f"microcloud_{x}{fname_suffix}" for x in possible_archs}
            for f in valid_names.intersection(tarball.getnames()):
                tarball.extract(f, path=tmp_dir)
                logger.debug(f"{f} was extracted from the tarball")
                self._stored.microcloud_binary_path = f"{tmp_dir}/{f}"
                break
            else:
                logger.debug("Missing arch specific binary from tarball")
            tarball.close()
        else:
            self._stored.microcloud_binary_path = microcloud_binary_resource

        if self._stored.microcloud_binary_path:
            self.snap_sideload_microcloud_binary()
            if tmp_dir:
                os.remove(self._stored.microcloud_binary_path)
                os.rmdir(tmp_dir)

    def snap_sideload_microcloud(self) -> None:
        """Side-load MicroCloud snap resource."""
        logger.debug("Applying MicroCloud snap side-load changes")

        # A 0 byte file will unload the resource
        if os.path.getsize(self._stored.microcloud_snap_path) == 0:
            logger.debug("Reverting to MicroCloud snap from snap store")
            channel: str = self._stored.config["snap-channel-microcloud"]
            try:
                cache = SnapCache()
                microcloud = cache["microcloud"]
                microcloud.ensure(SnapState.Latest, channel=channel)
            except SnapError as e:
                self.unit_blocked(f"Failed to refresh the MicroCloud snap: {e.message}")
                raise RuntimeError

        else:
            logger.debug("Side-loading MicroCloud snap")
            try:
                snap_install_local(self._stored.microcloud_snap_path, dangerous=True)
            except SnapError as e:
                self.unit_blocked(f"Failed to side-load MicroCloud snap: {e.message}")
                raise RuntimeError

            try:
                # Since the side-loaded snap doesn't have an assertion, some things need
                # to be done manually
                subprocess.run(
                    ["systemctl", "enable", "--now", "snap.microcloud.daemon.unix.socket"],
                    capture_output=True,
                    check=True,
                    timeout=600,
                )
            except subprocess.CalledProcessError as e:
                self.unit_blocked(f'Failed to run "{e.cmd}": {e.stderr} ({e.returncode})')
                raise RuntimeError
            except subprocess.TimeoutExpired as e:
                self.unit_blocked(f'Timeout exceeded while running "{e.cmd}"')
                raise RuntimeError

    def snap_sideload_microcloud_binary(self) -> None:
        """Side-load MicroCloud binary resource."""
        logger.debug("Applying MicroCloud binary side-load changes")
        microcloud_debug: str = "/var/snap/microcloud/common/microcloud.debug"

        # A 0 byte file will unload the resource
        if os.path.getsize(self._stored.microcloud_binary_path) == 0:
            logger.debug("Unloading side-loaded MicroCloud binary")
            if os.path.exists(microcloud_debug):
                os.remove(microcloud_debug)
        else:
            logger.debug("Side-loading MicroCloud binary")
            # Avoid "Text file busy" error
            if os.path.exists(microcloud_debug):
                logger.debug("Removing old side-loaded LXD binary")
                os.remove(microcloud_debug)
            shutil.copyfile(self._stored.microcloud_binary_path, microcloud_debug)
            os.chmod(microcloud_debug, 0o755)

        self.microcloud_reload()

    def unit_active(self, msg: str = "") -> None:
        """Set the unit's status to active and log the provided message, if any."""
        self.unit.status = ActiveStatus()
        if msg:
            logger.debug(msg)

    def unit_blocked(self, msg: str) -> None:
        """Set the unit's status to blocked and log the provided message."""
        self.unit.status = BlockedStatus(msg)
        logger.error(msg)

    def unit_maintenance(self, msg: str) -> None:
        """Set the unit's status to maintenance and log the provided message."""
        self.unit.status = MaintenanceStatus(msg)
        logger.info(msg)

    def unit_waiting(self, msg: str) -> None:
        """Set the unit's status to waiting and log the provided message."""
        self.unit.status = WaitingStatus(msg)
        logger.info(msg)


if __name__ == "__main__":
    main(MaasMicroCloudCharm)
