Synork custom panel for HA frontend — embeds the Synork Home React app as an
iframe panel inside HA's frontend for users who prefer a single-window experience.

## Deployment

This directory is the deployment target for the frontend feature module
(built in Session 6). The addon's install_frontend.py copies the built
React app into /config/www/synork-panel/ and registers it as an HA panel.

## Files (deployed by Session 6 build)

- index.html — panel entry point
- assets/ — bundled JS/CSS from the React build

## How it works

HA supports custom panels via `panel_custom` in configuration.yaml.
The Synork panel is registered as:

```yaml
panel_custom:
  - name: synork-home-panel
    url_path: synork
    sidebar_title: Synork Home
    sidebar_icon: mdi:home-assistant
    module_url: /local/synork-panel/synork-panel.js
    embed_iframe: true
    config:
      url: /local/synork-panel/index.html
```

The panel JS module creates an iframe pointing to the React app.
This keeps the React app fully isolated from HA's frontend.
