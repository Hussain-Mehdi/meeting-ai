#!/bin/zsh
set -e
cd "${0:A:h}/.."
mkdir -p bin
mkdir -p .build/module-cache
xcrun swiftc native/MeetingAudioCapture.swift -parse-as-library -O -module-cache-path .build/module-cache -framework ScreenCaptureKit -framework AVFoundation -framework CoreMedia -o bin/meeting-audio-capture
echo "Built bin/meeting-audio-capture"
