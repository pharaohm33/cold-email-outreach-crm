#!/bin/bash
curl -s -X POST http://127.0.0.1:8765/loop/resume -d '{}' && echo && echo "Resumed."
