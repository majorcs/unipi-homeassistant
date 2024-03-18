#!/usr/bin/with-contenv bashio

params="--mqtt-ip $(bashio::config 'mqtt')"
for unipi in $(bashio::config 'unipi')
do
	params="${params} --unipi-ip ${unipi}"
done

python3 /unipi2ha.py ${params}
