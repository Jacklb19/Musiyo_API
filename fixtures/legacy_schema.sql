
CREATE TABLE elementos (
	id VARCHAR(100) NOT NULL,
	titulo VARCHAR(250) NOT NULL,
	descripcion TEXT NOT NULL,
	interpretacion TEXT NOT NULL,
	estado_aprobacion VARCHAR(20) NOT NULL,
	aprobado_en DATETIME,
	autorizado_desde DATETIME,
	autorizado_hasta DATETIME,
	revocado_en DATETIME,
	creditos TEXT NOT NULL,
	fuentes TEXT NOT NULL,
	restricciones TEXT NOT NULL,
	PRIMARY KEY (id)
)

;

CREATE TABLE salas (
	id VARCHAR(100) NOT NULL,
	recorrido_id VARCHAR(100) NOT NULL,
	orden INTEGER NOT NULL,
	PRIMARY KEY (id)
)

;

CREATE TABLE puntos (
	id VARCHAR(100) NOT NULL,
	sala_id VARCHAR(100) NOT NULL,
	orden INTEGER NOT NULL,
	PRIMARY KEY (id),
	FOREIGN KEY(sala_id) REFERENCES salas (id)
)

;

CREATE TABLE recursos (
	id VARCHAR(100) NOT NULL,
	elemento_id VARCHAR(100) NOT NULL,
	nombre VARCHAR(250) NOT NULL,
	tipo_mime VARCHAR(100) NOT NULL,
	ruta_privada TEXT NOT NULL,
	aprobado BOOLEAN NOT NULL,
	autorizado_desde DATETIME,
	autorizado_hasta DATETIME,
	revocado_en DATETIME,
	PRIMARY KEY (id),
	FOREIGN KEY(elemento_id) REFERENCES elementos (id)
)

;

CREATE TABLE punto_elementos (
	punto_id VARCHAR(100) NOT NULL,
	elemento_id VARCHAR(100) NOT NULL,
	orden INTEGER NOT NULL,
	PRIMARY KEY (punto_id, elemento_id),
	FOREIGN KEY(punto_id) REFERENCES puntos (id),
	FOREIGN KEY(elemento_id) REFERENCES elementos (id)
)

;
CREATE INDEX ix_salas_recorrido_id ON salas (recorrido_id);
