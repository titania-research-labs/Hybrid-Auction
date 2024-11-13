# Use the official Rust image
FROM rust:latest

# install tools
RUN apt-get update && apt-get install -y \
    build-essential \
    curl \
    git \
    && rm -rf /var/lib/apt/lists/*

# install Foundry
RUN curl -L https://foundry.paradigm.xyz | bash

# set PATH
ENV PATH="/root/.foundry/bin:${PATH}"

# setup Foundry
RUN foundryup

# set working directory
WORKDIR /workspace
