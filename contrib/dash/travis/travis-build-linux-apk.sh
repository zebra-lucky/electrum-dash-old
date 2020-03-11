#!/bin/bash
set -ev

cd build
if [[ -n $TRAVIS_TAG ]]; then
    BUILD_REPO_URL=https://github.com/akhavr/electrum-dash.git
    git clone --branch $TRAVIS_TAG $BUILD_REPO_URL electrum-dash
else
    git clone .. electrum-dash
fi

pushd electrum-dash
./contrib/make_locale
find . -name '*.po' -delete
find . -name '*.pot' -delete
popd

DOCKER_CMD="rm -rf packages"
DOCKER_CMD="$DOCKER_CMD && ./contrib/make_packages"
DOCKER_CMD="$DOCKER_CMD && rm -rf packages/bls_py"
DOCKER_CMD="$DOCKER_CMD && rm -rf packages/python_bls*"
DOCKER_CMD="$DOCKER_CMD && ./contrib/make_apk"

if [[ $ELECTRUM_MAINNET == "false" ]]; then
    DOCKER_CMD="$DOCKER_CMD release-testnet"
fi

sudo chown -R 1000 electrum-dash
docker run --rm \
    -v $(pwd)/electrum-dash:/home/buildozer/build \
    -t zebralucky/electrum-dash-winebuild:Kivy33x bash -c \
    "$DOCKER_CMD"
