## [0.2.0-rc.2](https://github.com/SerBy48/BROKER-MQTT-SGPMP/compare/v0.2.0-rc.1...v0.2.0-rc.2) (2026-09-03)

### Bug Fixes

* **mosquitto:** MQTT_DEVICE_USERNAME/PASSWORD nunca llegaban al contenedor + entrypoint no era idempotente ([a36051f](https://github.com/SerBy48/BROKER-MQTT-SGPMP/commit/a36051f3a40082627e6f946b1cea0aee7de307f9))

## [0.2.0-rc.1](https://github.com/SerBy48/BROKER-MQTT-SGPMP/compare/v0.1.0...v0.2.0-rc.1) (2026-09-03)

### Features

* **mosquitto:** credencial MQTT dedicada para dispositivos IoT ([9d562b2](https://github.com/SerBy48/BROKER-MQTT-SGPMP/commit/9d562b218d9f7f1ba21f826da5c33dcbb0c2c48e))
* **rf23:** auth por credencial de BD, corrige ownership y agrega espera de ACK ([3489040](https://github.com/SerBy48/BROKER-MQTT-SGPMP/commit/3489040cf83b98350b280f82f951708eb39da9d9))

### Bug Fixes

* aiomqtt.Client ya no acepta client_id, es identifier ([5ae8c9e](https://github.com/SerBy48/BROKER-MQTT-SGPMP/commit/5ae8c9efa7ec2a425772917d9cf2e57b25fdeb0c))
* **dokploy:** default MQTT_PORT a 1884, el 1883 ya está ocupado por EMQX en el host compartido ([83391b0](https://github.com/SerBy48/BROKER-MQTT-SGPMP/commit/83391b04cdb99b2f199d8bc60d11dbf2ed1f3251))
* **mosquitto:** generar passwd automáticamente en el arranque, no depender del archivo gitignoreado ([6f444ac](https://github.com/SerBy48/BROKER-MQTT-SGPMP/commit/6f444ac5ea2d88ab9654f45bdb3118d4e39dfa0f))
* renombrar MQTT_PORT del host a MQTT_HOST_PORT, evita colisión con la del gateway ([453585c](https://github.com/SerBy48/BROKER-MQTT-SGPMP/commit/453585c21872a3831c063e193b559668b00d6022))
