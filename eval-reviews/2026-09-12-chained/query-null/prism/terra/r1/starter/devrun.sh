#!/bin/sh
cd "$(dirname "$0")"
prism run main.pr | head -n 1
