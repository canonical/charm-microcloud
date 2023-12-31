#!/bin/bash

HOST_PATH="/usr/share/maas/images"

if [ ! -d "$HOST_PATH" ]; then
    echo "Warning: Directory $HOST_PATH does not exist. Creating folder..."
    sudo mkdir -p $HOST_PATH
fi

# see here: https://maas.io/docs/mirroring-images-locally
KEYRING_FILE=/usr/share/keyrings/ubuntu-cloudimage-keyring.gpg
IMAGE_SRC=https://images.maas.io/ephemeral-v3/stable
IMAGE_DIR=/usr/share/maas/images/ephemeral-v3/stable

if [ -z "$(command -v sstream-mirror)" ]; then
    echo "sstream-mirror is not installed. Run 'sudo apt install sstream-mirror' to install it"
    exit 1
fi

sudo sstream-mirror \
    --keyring=$KEYRING_FILE $IMAGE_SRC $IMAGE_DIR \
    'arch=amd64' 'release~(jammy)' --max=1 --progress

sudo sstream-mirror \
    --keyring=$KEYRING_FILE $IMAGE_SRC $IMAGE_DIR \
    'os~(grub*|pxelinux)' --max=1 --progress

docker build -t nginx-maas-image-server .
docker run --name local-maas-image-server \
    -d -p 5000:80 \
    -v $HOST_PATH/ephemeral-v3/stable:/usr/share/maas/images/ephemeral-v3/stable:ro \
    nginx-maas-image-server