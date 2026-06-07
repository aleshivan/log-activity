#!/bin/bash

cd /home/alexhd/tmp/scarab/log
rsync -avz 'precision:/var/log/precision-8443*' .