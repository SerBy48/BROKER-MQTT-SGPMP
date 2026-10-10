## [0.2.0-rc.11](https://github.com/SerBy48/BROKER-MQTT-SGPMP/compare/v0.2.0-rc.10...v0.2.0-rc.11) (2026-10-10)

### Features

* **rf23:** aceptar fps en el comando de configuracion de camaras (RF-23 v1.1) ([5836c73](https://github.com/SerBy48/BROKER-MQTT-SGPMP/commit/5836c73e1a935b69ea0fc75075f426b7039c7eba))

### Bug Fixes

* **rf17:** conectada respeta el aviso de desconexion del edge (Arekkazu/sgpmp-backend[#532](https://github.com/SerBy48/BROKER-MQTT-SGPMP/issues/532)) ([662d18b](https://github.com/SerBy48/BROKER-MQTT-SGPMP/commit/662d18b93235931c65702330ed1679c585f8972a))

## [0.2.0-rc.10](https://github.com/SerBy48/BROKER-MQTT-SGPMP/compare/v0.2.0-rc.9...v0.2.0-rc.10) (2026-10-07)

### Features

* **rf17:** propagar umbrales ambientales al Gateway Edge por /v1/commands (INC-M09-104-G29) ([217f9ee](https://github.com/SerBy48/BROKER-MQTT-SGPMP/commit/217f9eea6efd13675d3e07c86c445b3151fdb142))

### Bug Fixes

* **rf17:** detectar al instante un Gateway Edge desconectado con el aviso DESCONEXION (TC-M09-63) ([caf0c60](https://github.com/SerBy48/BROKER-MQTT-SGPMP/commit/caf0c6015b02a90e20c0e9c436f7e23a1d6e1a53))

## [0.2.0-rc.9](https://github.com/SerBy48/BROKER-MQTT-SGPMP/compare/v0.2.0-rc.8...v0.2.0-rc.9) (2026-10-04)

### Features

* **rf23:** PENDIENTE al instante si el Gateway Edge no esta conectado al broker ([9213907](https://github.com/SerBy48/BROKER-MQTT-SGPMP/commit/9213907fd896777642918aa695a56c24ee5920bb))

## [0.2.0-rc.8](https://github.com/SerBy48/BROKER-MQTT-SGPMP/compare/v0.2.0-rc.7...v0.2.0-rc.8) (2026-10-04)

### Bug Fixes

* **heartbeat:** guardar estado_local_buffer en la columna "char" de modulo3.heartbeats ([270f70a](https://github.com/SerBy48/BROKER-MQTT-SGPMP/commit/270f70ade9a8a9ab033804ad3c2fd85cdbeed50e))

## [0.2.0-rc.7](https://github.com/SerBy48/BROKER-MQTT-SGPMP/compare/v0.2.0-rc.6...v0.2.0-rc.7) (2026-10-04)

### Features

* **rf23:** credencial del Gateway Edge con los seriales que atiende segun modulo9 (TC-M09-250/251) ([04bb366](https://github.com/SerBy48/BROKER-MQTT-SGPMP/commit/04bb36671af2d80e158235701af56f6fd125dcf2))
* **rf23:** credencial MQTT por Raspberry con dynamic-security (TC-M09-250/251) ([94f0387](https://github.com/SerBy48/BROKER-MQTT-SGPMP/commit/94f03875f58583cce8da24c23215b7d153612ff3))
* **rf23:** id_comando y emitido_en en comandos y validacion del ACK (TC-M09-G127) ([e2a7294](https://github.com/SerBy48/BROKER-MQTT-SGPMP/commit/e2a7294f049204a9e7a630eb8b3dd10e1e11d272))

### Bug Fixes

* **rf23:** exponer solo listeners TLS cuando el ambiente lo exige (TC-M09-G127) ([bd1910f](https://github.com/SerBy48/BROKER-MQTT-SGPMP/commit/bd1910f7311ca0355e7a9c69953f7ae27d995fc7))

## [0.2.0-rc.6](https://github.com/SerBy48/BROKER-MQTT-SGPMP/compare/v0.2.0-rc.5...v0.2.0-rc.6) (2026-09-21)

### Bug Fixes

* **dokploy:** comentar puertos tls por defecto para evitar colision en 8883 ([32644e8](https://github.com/SerBy48/BROKER-MQTT-SGPMP/commit/32644e84834ad50a4527e13a11ef32dfc33fa201))

## [0.2.0-rc.5](https://github.com/SerBy48/BROKER-MQTT-SGPMP/compare/v0.2.0-rc.4...v0.2.0-rc.5) (2026-09-17)

### Bug Fixes

* **mosquitto:** agregar acl_file con privilegio minimo por usuario ([869a315](https://github.com/SerBy48/BROKER-MQTT-SGPMP/commit/869a315d74f99ba6d956608078fd94851c3bf61d))

## [0.2.0-rc.4](https://github.com/SerBy48/BROKER-MQTT-SGPMP/compare/v0.2.0-rc.3...v0.2.0-rc.4) (2026-09-16)

### Bug Fixes

* **mosquitto:** soportar listeners TLS (mqtts/wss) en el broker ([741c065](https://github.com/SerBy48/BROKER-MQTT-SGPMP/commit/741c065716ea252dcac3426730e4a8735962d9bd))

## [0.2.0-rc.3](https://github.com/SerBy48/BROKER-MQTT-SGPMP/compare/v0.2.0-rc.2...v0.2.0-rc.3) (2026-09-04)

### Features

* **logging:** logs JSON detallados a stdout (MQTT/BD/API) sin depender de .env ([cfd91b4](https://github.com/SerBy48/BROKER-MQTT-SGPMP/commit/cfd91b437532f1975a23399b6cf064f597fc1022))

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
