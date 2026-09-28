# Musiyo API

Backend de Musiyo Bëtsknaté. Este repositorio alojará una API FastAPI con PostgreSQL para servir el catálogo, las fichas, los recorridos y los recursos culturales autorizados a la web y a Unity.

## Estado

El repositorio está preparado, pero la API todavía no se ha implementado. La web y el proyecto Unity se mantienen en repositorios separados.

## Responsabilidades previstas

- Publicar un contrato versionado para recorridos, salas, puntos y elementos con identificadores estables.
- Denegar por defecto las consultas de contenido o archivos sin aprobación cultural y autorización vigente.
- Guardar fichas, fuentes, créditos, restricciones y relaciones entre elementos y puntos; mantener privados los archivos y evidencias de consentimiento.
- Permitir al Validador Cultural autenticado corregir solo título, descripción e interpretación de fichas publicadas, registrando autor y fecha.
- Exponer al guía únicamente información aprobada y vigente; aislar las claves de IA y voz en el servidor.

Las pruebas iniciales usarán datos sintéticos. Ningún dato de ejemplo equivale a una autorización cultural real.
