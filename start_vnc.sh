#!/bin/bash
pkill -f Xvfb 2>/dev/null
pkill -f x11vnc 2>/dev/null
pkill -f websockify 2>/dev/null
pkill -f chrome 2>/dev/null
sleep 1

Xvfb :99 -screen 0 1280x720x24 &
export DISPLAY=:99
sleep 1

mkdir -p ~/.vnc
x11vnc -storepasswd browser123 ~/.vnc/passwd 2>/dev/null
x11vnc -display :99 -rfbauth ~/.vnc/passwd -forever -shared -bg 2>/dev/null

websockify --web /usr/share/novnc 6080 localhost:5900 &
sleep 1

IP=$(hostname -I | awk '{print $1}')
echo "READY — open http://${IP}:6080/vnc.html"
echo "Password: browser123"
