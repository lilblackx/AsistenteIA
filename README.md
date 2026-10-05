# AsistenteIA

Asistente de texto para Windows pensado para soporte técnico de un ISP. Selecciona texto en cualquier aplicación, pulsa un atajo de teclado y un modelo de IA (Gemini, con respaldo en Groq) corrige, reformula o redacta la respuesta.

Corre en segundo plano con un icono en la bandeja del sistema.

## Atajos

| Tecla | Modo | Resultado |
|-------|------|-----------|
| `F1` | Corregir ortografía | Reemplaza el texto seleccionado |
| `F2` | Sugerir respuesta a cliente | Abre ventana con la respuesta propuesta |
| `F3` | Reformular más cordial | Reemplaza el texto seleccionado |
| `F4` | Instrucción libre | Pide una instrucción y abre ventana con el resultado |
| `F8` | Resumen para ticket | Abre ventana con una nota interna para el ticket |
| `Ctrl+F1` | Salir | Cierra el asistente |

En las ventanas de resultado el texto ya queda copiado al portapapeles. Botones: Copiar y cerrar, Reemplazar, Regenerar y Cerrar. `Ctrl+Enter` copia y cierra, `Esc` cierra.

## Requisitos

- Windows 10 o superior
- Python 3.13 (solo para ejecutar desde código o compilar)
- Una API key de [Gemini](https://aistudio.google.com/apikey)
- Opcional: una API key de [Groq](https://console.groq.com/keys) como respaldo

## Instalación

```bat
pip install -r requirements.txt
```

Crea un archivo `.env` junto a `assistant.py`:

```
GEMINI_API_KEY=tu_clave_de_gemini
GROQ_API_KEY=tu_clave_de_groq
```

`GROQ_API_KEY` solo es necesaria si usas modelos con prefijo `groq:`. El archivo `.env` no se sube al repositorio.

## Uso

Ejecutar desde código:

```bat
iniciar.bat
```

Compilar un `.exe` portable (requiere PyInstaller):

```bat
pip install pyinstaller
build.bat
```

El resultado queda en la carpeta `release`, junto con `config.json` y `.env`. El ejecutable lee ambos archivos desde su propia carpeta.

Al primer arranque el asistente se registra para iniciar con Windows. Se puede desactivar desde el menú del icono de la bandeja ("Iniciar con Windows").

## Configuración

Todo se define en `config.json`:

- `models`: lista de modelos en orden de prioridad. Si uno falla o está saturado, se prueba el siguiente. Los modelos con prefijo `groq:` usan la API de Groq; el resto usa Gemini.
- `modes`: cada modo tiene `name`, `hotkey`, `prompt` y `output` (`replace` pega el resultado en lugar de la selección; `popup` abre una ventana). Con `"ask": true` el modo pide una instrucción antes de consultar.
- `quit_hotkey`: atajo para cerrar el asistente.
- `daily_warning`: número de consultas diarias a partir del cual se muestra un aviso de límite gratuito.

Para añadir o cambiar un modo basta con editar `modes` y reiniciar el asistente.

## Archivos generados

- `assistant.log`: registro de ejecución (accesible desde el menú de la bandeja).
- `usage.json`: contador de consultas por modelo del día actual.
- `.autostart_configured`: marca de que el arranque con Windows ya se configuró.

## Notas

- Los atajos se capturan globalmente y la aplicación activa no los recibe. Por eso `F1` ya no abre la ayuda en otros programas mientras el asistente está activo.
- Solo se permite una instancia a la vez.
- El texto seleccionado se envía a Gemini o a Groq. No uses el asistente con información que no deba salir de la empresa.
