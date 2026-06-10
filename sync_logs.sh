#!/bin/bash
# Sincroniza los logs de los cuatro sistemas desde el servidor al directorio log/
# (que es de donde leen todos los perfiles en profiles/*.config).
# Nota: asume que los cuatro viven bajo /var/log en el host 'precision'. Si alguno
# está en otra ruta/host, ajusta la línea correspondiente.

cd /home/alexhd/tmp/scarab/log || exit 1

rsync -avz \
  'precision:/var/log/dashboard-8443*' \
  'precision:/var/log/precision-8443*' \
  'precision:/var/log/precision-adm-9090*' \
  'precision:/var/log/precision-mbl-9450*' \
  log/
