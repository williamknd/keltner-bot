"""
Keltner Channel Flipped (Mean Reversion) Bot - Bybit + Dashboard
Timeframe: 5min | Stop Loss: 1.5% | Hold: 120 candles
"""
import os, time, logging, json, threading
from datetime import datetime
from pybit.unified_trading import HTTP
from http.server import HTTPServer, BaseHTTPRequestHandler

# ── CONFIG ──────────────────────────────────────────
API_KEY       = os.environ.get("API_KEY", "")
API_SECRET    = os.environ.get("API_SECRET", "")
TESTNET       = os.environ.get("TESTNET", "true").lower() == "true"
SYMBOL        = os.environ.get("SYMBOL", "BTCUSDT")
CATEGORY      = "linear"
TIMEFRAME     = "5"
QTY           = os.environ.get("QTY", "0.001")
LEVERAGE      = int(os.environ.get("LEVERAGE", "1"))
STOP_LOSS_PCT = 0.015  # 1.5%
MAX_CANDLES   = 120
LOOP_INTERVAL = int(os.environ.get("LOOP_INTERVAL", "30"))
PORT          = int(os.environ.get("PORT", "8080"))

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
log = logging.getLogger("KeltnerBot")

if not API_KEY or not API_SECRET:
    log.error("API_KEY e API_SECRET nao configurados!")
    exit(1)

session = HTTP(testnet=TESTNET, api_key=API_KEY, api_secret=API_SECRET)

# ── ESTADO GLOBAL ────────────────────────────────────
state = {
    "status": "AGUARDANDO",
    "price": 0,
    "upper": 0,
    "lower": 0,
    "mid": 0,
    "signal": "NENHUM",
    "position": None,
    "trades": [],
    "wins": 0,
    "losses": 0,
    "candles_held": 0,
    "last_update": "",
    "testnet": TESTNET,
    "symbol": SYMBOL,
    "recent_candles": []
}

# ── ESTRATÉGIA ───────────────────────────────────────
def ema(values, period):
    result = [values[0]] * len(values)
    k = 2 / (period + 1)
    for i in range(1, len(values)):
        result[i] = values[i] * k + result[i-1] * (1 - k)
    return result

def atr(highs, lows, closes, period=10):
    tr = [highs[0] - lows[0]] * len(closes)
    for i in range(1, len(closes)):
        tr[i] = max(highs[i]-lows[i], abs(highs[i]-closes[i-1]), abs(lows[i]-closes[i-1]))
    return ema(tr, period)

def get_signal(closes, highs, lows):
    if len(closes) < 25: return 0, 0, 0, 0
    mid_vals  = ema(closes, 20)
    atr_vals  = atr(highs, lows, closes)
    i         = len(closes) - 1
    upper     = mid_vals[i] + 1.5 * atr_vals[i]
    lower     = mid_vals[i] - 1.5 * atr_vals[i]
    mid       = mid_vals[i]
    if closes[i] > upper: return -1, upper, lower, mid   # SHORT
    if closes[i] < lower: return  1, upper, lower, mid   # LONG
    return 0, upper, lower, mid

def fetch_candles(limit=250):
    resp = session.get_kline(category=CATEGORY, symbol=SYMBOL, interval=TIMEFRAME, limit=limit)
    candles = list(reversed(resp["result"]["list"]))
    opens  = [float(c[1]) for c in candles]
    highs  = [float(c[2]) for c in candles]
    lows   = [float(c[3]) for c in candles]
    closes = [float(c[4]) for c in candles]
    times  = [int(c[0])   for c in candles]
    state["recent_candles"] = [{"o": float(c[1]), "c": float(c[4]), "h": float(c[2]), "l": float(c[3])} for c in candles[-20:]]
    return opens, highs, lows, closes, times

def get_last_price():
    return float(session.get_tickers(category=CATEGORY, symbol=SYMBOL)["result"]["list"][0]["lastPrice"])

def get_position():
    for p in session.get_positions(category=CATEGORY, symbol=SYMBOL)["result"]["list"]:
        if float(p["size"]) > 0: return p
    return None

def set_leverage():
    try:
        session.set_leverage(category=CATEGORY, symbol=SYMBOL,
                             buyLeverage=str(LEVERAGE), sellLeverage=str(LEVERAGE))
    except: pass

def open_position(side, price):
    sl = round(price * (1 - STOP_LOSS_PCT if side == "Buy" else 1 + STOP_LOSS_PCT), 4)
    try:
        session.place_order(category=CATEGORY, symbol=SYMBOL, side=side,
                            orderType="Market", qty=QTY, stopLoss=str(sl), timeInForce="GTC")
        log.info(f"ABRIU {side} | Preco: {price} | SL: {sl}")
        state["trades"].append({"type": side, "entry": price, "sl": sl,
                                 "time": datetime.now().strftime("%H:%M:%S"),
                                 "result": "ABERTA", "exit": None, "pnl": None})
        state["position"] = {"side": side, "entry": price, "sl": sl}
        state["status"]   = "LONG ATIVO" if side == "Buy" else "SHORT ATIVO"
        return True
    except Exception as e:
        log.error(f"Erro ao abrir: {e}")
        return False

def close_position(position):
    side = "Sell" if position["side"] == "Buy" else "Buy"
    try:
        price = get_last_price()
        session.place_order(category=CATEGORY, symbol=SYMBOL, side=side,
                            orderType="Market", qty=position["size"], reduceOnly=True)
        entry = state["position"]["entry"] if state["position"] else price
        pnl   = (price - entry) if position["side"] == "Buy" else (entry - price)
        won   = pnl > 0
        if won: state["wins"] += 1
        else:   state["losses"] += 1
        if state["trades"]:
            state["trades"][-1]["result"] = "WIN" if won else "LOSS"
            state["trades"][-1]["exit"]   = price
            state["trades"][-1]["pnl"]    = round(pnl, 4)
        state["position"] = None
        state["status"]   = "AGUARDANDO"
        log.info(f"FECHOU | {'WIN' if won else 'LOSS'} | PnL: {pnl:.4f}")
        return True
    except Exception as e:
        log.error(f"Erro ao fechar: {e}")
        return False

# ── DASHBOARD HTML ───────────────────────────────────
DASHBOARD_HTML = """<!DOCTYPE html>
<html lang="pt-BR">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1.0">
<title>Keltner Bot Dashboard</title>
<link href="https://fonts.googleapis.com/css2?family=Share+Tech+Mono&family=Exo+2:wght@300;600;800&display=swap" rel="stylesheet">
<style>
*{margin:0;padding:0;box-sizing:border-box}
body{background:#050a0f;color:#e0f0ff;font-family:'Exo 2',sans-serif;min-height:100vh;padding:20px}
body::before{content:'';position:fixed;inset:0;background:radial-gradient(ellipse at 20% 50%,#0a1628 0%,#050a0f 60%);z-index:-1}
h1{font-size:1.8rem;font-weight:800;letter-spacing:4px;text-transform:uppercase;color:#06b6d4;text-shadow:0 0 20px #06b6d455;margin-bottom:4px}
.subtitle{font-family:'Share Tech Mono';font-size:.75rem;color:#4a7a9b;letter-spacing:2px;margin-bottom:24px}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:12px;margin-bottom:20px}
.card{background:#0a1628;border:1px solid #0d2540;border-radius:12px;padding:16px;position:relative;overflow:hidden}
.card::before{content:'';position:absolute;top:0;left:0;right:0;height:2px;background:linear-gradient(90deg,transparent,#06b6d444,transparent)}
.card-label{font-size:.65rem;letter-spacing:2px;text-transform:uppercase;color:#4a7a9b;margin-bottom:8px}
.card-value{font-family:'Share Tech Mono';font-size:1.4rem;font-weight:700;color:#06b6d4}
.card-value.green{color:#00ff88}.card-value.red{color:#ff4466}.card-value.yellow{color:#ffd700}
.card-value.upper{color:#ff4466}.card-value.lower{color:#00ff88}
.winrate-bar{height:6px;background:#0d2540;border-radius:3px;margin-top:6px;overflow:hidden}
.winrate-fill{height:100%;background:linear-gradient(90deg,#06b6d4,#00ff88);border-radius:3px;transition:width .5s}
.section-title{font-size:.7rem;letter-spacing:3px;text-transform:uppercase;color:#4a7a9b;margin:20px 0 10px}
.candles{display:flex;gap:3px;align-items:flex-end;height:100px;background:#0a1628;border-radius:8px;padding:8px;border:1px solid #0d2540;position:relative}
.candle-wrap{display:flex;flex-direction:column;align-items:center;flex:1;height:100%}
.candle-body{width:8px;border-radius:2px;min-height:4px}
.candle-body.green{background:#00ff88}.candle-body.red{background:#ff4466}
.upper-line{position:absolute;left:8px;right:8px;height:1px;background:#ff446688;border-top:1px dashed #ff4466}
.lower-line{position:absolute;left:8px;right:8px;height:1px;background:#00ff8888;border-top:1px dashed #00ff88}
.mid-line{position:absolute;left:8px;right:8px;height:1px;background:#06b6d488;border-top:1px dashed #06b6d4}
.trades-list{background:#0a1628;border-radius:12px;border:1px solid #0d2540;overflow:hidden}
.trade-row{display:grid;grid-template-columns:60px 50px 90px 90px 80px 70px;gap:8px;padding:10px 16px;border-bottom:1px solid #060e1a;font-family:'Share Tech Mono';font-size:.75rem;align-items:center}
.trade-row.header{background:#060e1a;color:#4a7a9b;font-size:.65rem;letter-spacing:1px}
.badge{padding:2px 8px;border-radius:10px;font-size:.65rem;font-weight:600}
.badge.win{background:#003322;color:#00ff88}.badge.loss{background:#330011;color:#ff4466}
.badge.open{background:#001a1f;color:#06b6d4}.badge.buy{background:#003322;color:#00ff88}.badge.sell{background:#330011;color:#ff4466}
.last-update{font-family:'Share Tech Mono';font-size:.65rem;color:#2a4a6b;margin-top:16px;text-align:right}
</style>
</head>
<body>
<h1>Keltner Bot</h1>
<div class="subtitle" id="subtitle">carregando...</div>
<div class="grid">
  <div class="card"><div class="card-label">Status</div><div id="status" class="card-value">--</div></div>
  <div class="card"><div class="card-label">Preco</div><div id="price" class="card-value">--</div></div>
  <div class="card"><div class="card-label">Banda Superior</div><div id="upper" class="card-value upper">--</div></div>
  <div class="card"><div class="card-label">EMA (meio)</div><div id="mid" class="card-value">--</div></div>
  <div class="card"><div class="card-label">Banda Inferior</div><div id="lower" class="card-value lower">--</div></div>
  <div class="card"><div class="card-label">Sinal</div><div id="signal" class="card-value">--</div></div>
  <div class="card"><div class="card-label">Wins</div><div id="wins" class="card-value green">--</div></div>
  <div class="card"><div class="card-label">Losses</div><div id="losses" class="card-value red">--</div></div>
  <div class="card"><div class="card-label">Win Rate</div><div id="winrate" class="card-value yellow">--%</div><div class="winrate-bar"><div class="winrate-fill" id="winrate-bar" style="width:0%"></div></div></div>
  <div class="card"><div class="card-label">Velas Abertas</div><div id="candles_held" class="card-value">--</div></div>
</div>
<div class="section-title">Ultimas 20 Velas + Canal Keltner</div>
<div class="candles" id="candles">
  <div class="upper-line" id="upper-line" style="top:10%"></div>
  <div class="mid-line"   id="mid-line"   style="top:50%"></div>
  <div class="lower-line" id="lower-line" style="top:90%"></div>
</div>
<div class="section-title">Historico de Trades</div>
<div class="trades-list">
  <div class="trade-row header"><span>Hora</span><span>Tipo</span><span>Entrada</span><span>Saida</span><span>PnL</span><span>Result</span></div>
  <div id="trades"></div>
</div>
<div class="last-update">Atualizado: <span id="last_update">--</span></div>
<script>
async function update(){
  try{
    const r=await fetch('/api');const d=await r.json();
    document.getElementById('subtitle').textContent=(d.testnet?'TESTNET':'MAINNET')+' | '+d.symbol+' | SL: 1.5% | Hold: 120 velas | EMA20 + ATR10';
    const st=document.getElementById('status');st.textContent=d.status;
    st.className='card-value '+(d.status.includes('LONG')?'green':d.status.includes('SHORT')?'red':'');
    document.getElementById('price').textContent='$'+d.price.toFixed(2);
    document.getElementById('upper').textContent='$'+d.upper.toFixed(2);
    document.getElementById('mid').textContent='$'+d.mid.toFixed(2);
    document.getElementById('lower').textContent='$'+d.lower.toFixed(2);
    const sg=document.getElementById('signal');sg.textContent=d.signal;
    sg.className='card-value '+(d.signal=='LONG'?'green':d.signal=='SHORT'?'red':'');
    document.getElementById('wins').textContent=d.wins;
    document.getElementById('losses').textContent=d.losses;
    const total=d.wins+d.losses;const wr=total>0?Math.round(d.wins/total*100):0;
    document.getElementById('winrate').textContent=wr+'%';
    document.getElementById('winrate-bar').style.width=wr+'%';
    document.getElementById('candles_held').textContent=d.candles_held+'/120';
    document.getElementById('last_update').textContent=d.last_update;
    const cc=document.getElementById('candles');
    const upperLine=document.getElementById('upper-line');
    const midLine=document.getElementById('mid-line');
    const lowerLine=document.getElementById('lower-line');
    const existing=[...cc.children].filter(c=>c!==upperLine&&c!==midLine&&c!==lowerLine);
    existing.forEach(c=>c.remove());
    if(d.recent_candles&&d.recent_candles.length){
      const allH=[...d.recent_candles.map(c=>c.h),d.upper];
      const allL=[...d.recent_candles.map(c=>c.l),d.lower];
      const maxH=Math.max(...allH);const minL=Math.min(...allL);const range=maxH-minL||1;
      d.recent_candles.forEach(c=>{
        const isGreen=c.c>=c.o;
        const bodyH=Math.max(4,Math.abs(c.c-c.o)/range*80);
        const w=document.createElement('div');w.className='candle-wrap';
        const b=document.createElement('div');b.className='candle-body '+(isGreen?'green':'red');
        b.style.height=bodyH+'px';b.style.marginTop='auto';
        w.appendChild(b);cc.appendChild(w);
      });
      if(d.upper>0){
        const uPct=100-(d.upper-minL)/range*100;
        const mPct=100-(d.mid-minL)/range*100;
        const lPct=100-(d.lower-minL)/range*100;
        upperLine.style.top=Math.min(95,Math.max(2,uPct))+'%';
        midLine.style.top=Math.min(95,Math.max(2,mPct))+'%';
        lowerLine.style.top=Math.min(95,Math.max(2,lPct))+'%';
      }
    }
    const tl=document.getElementById('trades');tl.innerHTML='';
    const trades=[...d.trades].reverse().slice(0,10);
    if(!trades.length){tl.innerHTML='<div style="padding:16px;text-align:center;color:#2a4a6b;font-size:.75rem;font-family:Share Tech Mono">Nenhum trade ainda</div>';}
    trades.forEach(t=>{
      const row=document.createElement('div');row.className='trade-row';
      const res=t.result==='WIN'?'win':t.result==='LOSS'?'loss':'open';
      const tipo=t.type==='Buy'?'buy':'sell';
      row.innerHTML=`<span>${t.time}</span><span><span class="badge ${tipo}">${t.type==='Buy'?'LONG':'SHORT'}</span></span><span>$${t.entry.toFixed(2)}</span><span>${t.exit?'$'+t.exit.toFixed(2):'-'}</span><span style="color:${t.pnl>0?'#00ff88':t.pnl<0?'#ff4466':'#4a7a9b'}">${t.pnl!=null?(t.pnl>0?'+':'')+t.pnl.toFixed(4):'-'}</span><span><span class="badge ${res}">${t.result}</span></span>`;
      tl.appendChild(row);
    });
  }catch(e){console.log(e)}
}
update();setInterval(update,5000);
</script>
</body>
</html>"""

# ── SERVIDOR WEB ─────────────────────────────────────
class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args): pass
    def do_GET(self):
        if self.path == '/api':
            data = json.dumps(state).encode()
            self.send_response(200)
            self.send_header('Content-Type','application/json')
            self.send_header('Access-Control-Allow-Origin','*')
            self.end_headers()
            self.wfile.write(data)
        else:
            self.send_response(200)
            self.send_header('Content-Type','text/html; charset=utf-8')
            self.end_headers()
            self.wfile.write(DASHBOARD_HTML.encode())

def start_server():
    server = HTTPServer(('0.0.0.0', PORT), Handler)
    log.info(f"Dashboard na porta {PORT}")
    server.serve_forever()

# ── LOOP PRINCIPAL ───────────────────────────────────
def run():
    log.info(f"Keltner Bot | {SYMBOL} | Testnet: {TESTNET} | Qty: {QTY} | SL: 1.5% | Hold: 120 velas")
    set_leverage()
    threading.Thread(target=start_server, daemon=True).start()

    position_open_candle = None
    last_signal_candle   = None

    while True:
        try:
            opens, highs, lows, closes, timestamps = fetch_candles()
            if len(closes) < 25:
                log.warning(f"Velas insuficientes: {len(closes)}. Aguardando...")
                time.sleep(LOOP_INTERVAL)
                continue

            signal, upper, lower, mid = get_signal(closes, highs, lows)
            price  = get_last_price()
            now_ts = timestamps[-1]

            state["price"]       = price
            state["upper"]       = round(upper, 4)
            state["lower"]       = round(lower, 4)
            state["mid"]         = round(mid, 4)
            state["signal"]      = "LONG" if signal==1 else "SHORT" if signal==-1 else "NENHUM"
            state["last_update"] = datetime.now().strftime("%H:%M:%S")

            log.info(f"Preco: {price:.2f} | Upper: {upper:.2f} | Lower: {lower:.2f} | Sinal: {state['signal']}")

            position = get_position()
            if position:
                if position_open_candle:
                    candles_held = sum(1 for t in timestamps if t > position_open_candle)
                    state["candles_held"] = candles_held
                    log.info(f"{position['side']} ativo | Velas: {candles_held}/{MAX_CANDLES}")
                    if candles_held >= MAX_CANDLES:
                        if close_position(position):
                            position_open_candle = None
                            state["candles_held"] = 0
            else:
                state["candles_held"] = 0
                state["position"]     = None
                state["status"]       = "AGUARDANDO"
                if signal != 0 and now_ts != last_signal_candle:
                    side = "Buy" if signal == 1 else "Sell"
                    log.info(f"{'LONG' if signal==1 else 'SHORT'} detectado!")
                    if open_position(side, price):
                        position_open_candle = now_ts
                        last_signal_candle   = now_ts

        except Exception as e:
            log.error(f"Erro: {e}")

        time.sleep(LOOP_INTERVAL)

if __name__ == "__main__":
    run()
