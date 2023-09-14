# MicroCloud charm for MAAS

This charm deploys MicroCloud on a MAAS cluster with Juju.

## Build the charm

* Install and run `tox` to setup the virtualenv and install the python dependencies.
* Install `charmcraft` to build the charm: `snap install charmcraft`
* Build the charm: `charmcraft pack`

## Setting up a MAAS cluster to deploy our charm

* You can follow the instructions in `setup/maas-setup.sh`