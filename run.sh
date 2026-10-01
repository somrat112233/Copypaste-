#!/data/data/com.termux/files/usr/bin/bash
termux-wake-lock 2>/dev/null || true
cd "$HOME/copypaste"
if [ -z "$BOT_TOKEN" ]; then
  echo "❌ BOT_TOKEN set koro age:  export BOT_TOKEN='xxxxx'"
  exit 1
fi
nohup python agent.py > agent.log 2>&1 &
echo "✅ copypaste started. PID: $!"
echo "   Log  : tail -f ~/copypaste/agent.log"
echo "   Stop : pkill -f agent.py"
