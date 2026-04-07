const { useEffect, useMemo, useRef, useState } = React;

const STATUS = {
  SUCCESS: "SUCCESS",
  MISMATCH: "MISMATCH",
};

function formatAmount(value) {
  if (value === null || value === undefined || Number.isNaN(Number(value))) {
    return "0";
  }
  const number = Number(value);
  const fixed = number.toFixed(2);
  return fixed.endsWith(".00") ? fixed.slice(0, -3) : fixed;
}

function formatClock(totalSeconds) {
  const sec = Math.max(0, totalSeconds);
  const minPart = Math.floor(sec / 60);
  const secPart = String(sec % 60).padStart(2, "0");
  return `${minPart}:${secPart}`;
}

function generateMerchantId() {
  return `merchant_${Math.random().toString(36).slice(2, 10)}`;
}

function playSound(type) {
  try {
    const ctx = new (window.AudioContext || window.webkitAudioContext)();
    if (type === "success") {
      [523, 659, 784, 1047].forEach((freq, idx) => {
        const osc = ctx.createOscillator();
        const gain = ctx.createGain();
        osc.connect(gain);
        gain.connect(ctx.destination);
        osc.frequency.value = freq;
        osc.type = "sine";
        const t = ctx.currentTime + idx * 0.12;
        gain.gain.setValueAtTime(0.28, t);
        gain.gain.exponentialRampToValueAtTime(0.001, t + 0.35);
        osc.start(t);
        osc.stop(t + 0.35);
      });
    } else {
      [200, 150, 200, 150].forEach((freq, idx) => {
        const osc = ctx.createOscillator();
        const gain = ctx.createGain();
        osc.connect(gain);
        gain.connect(ctx.destination);
        osc.frequency.value = freq;
        osc.type = "sawtooth";
        const t = ctx.currentTime + idx * 0.18;
        gain.gain.setValueAtTime(0.35, t);
        gain.gain.exponentialRampToValueAtTime(0.001, t + 0.14);
        osc.start(t);
        osc.stop(t + 0.14);
      });
    }
  } catch (err) {
    console.warn("Audio unavailable", err);
  }
}

function App() {
  const [merchantId, setMerchantId] = useState(localStorage.getItem("upiguard_merchant_id") || "");
  const [merchantName, setMerchantName] = useState(localStorage.getItem("upiguard_merchant_name") || "");
  const [loginName, setLoginName] = useState("");

  const [view, setView] = useState("terminal");
  const [connected, setConnected] = useState(false);

  const [amountStr, setAmountStr] = useState("");
  const [screen, setScreen] = useState("amount");

  const [currentQrId, setCurrentQrId] = useState("");
  const [currentAmount, setCurrentAmount] = useState(0);
  const [qrImage, setQrImage] = useState("");
  const [isDemoMode, setIsDemoMode] = useState(false);
  const [demoUpiId, setDemoUpiId] = useState("demo@upi");
  const [paymentMode, setPaymentMode] = useState(localStorage.getItem("upiguard_payment_mode") || "live");
  const [qrError, setQrError] = useState("");

  const [timerRemaining, setTimerRemaining] = useState(0);
  const [timerTotal, setTimerTotal] = useState(0);

  const [history, setHistory] = useState([]);

  const [analytics, setAnalytics] = useState({
    summary: {
      total_transactions: 0,
      success_rate: 0,
      fraud_count: 0,
    },
    charts: {
      daily_revenue: { labels: [], data: [] },
      hourly_distribution: { labels: [], data: [] },
    },
  });

  const [auditLogs, setAuditLogs] = useState([]);
  const [auditAction, setAuditAction] = useState("ALL");
  const [auditStatus, setAuditStatus] = useState("ALL");

  const [result, setResult] = useState({
    open: false,
    status: STATUS.SUCCESS,
    paid: 0,
    expected: 0,
    upi_id: "",
    transaction_id: "",
    fraud_reasons: [],
  });
  const [resultCountdown, setResultCountdown] = useState(6);

  const [creatingQr, setCreatingQr] = useState(false);

  const socketRef = useRef(null);
  const merchantIdRef = useRef(merchantId);
  const viewRef = useRef(view);

  const revenueCanvasRef = useRef(null);
  const hoursCanvasRef = useRef(null);
  const revenueChartRef = useRef(null);
  const hoursChartRef = useRef(null);

  const merchantAvatar = useMemo(() => {
    if (!merchantName) return "M";
    return merchantName[0].toUpperCase();
  }, [merchantName]);

  const merchantVpa = useMemo(() => {
    if (!merchantName) return "merchant@okaxis";
    const prefix = merchantName.toLowerCase().replace(/[^a-z0-9]/g, "").slice(0, 10) || "merchant";
    return `${prefix}@okaxis`;
  }, [merchantName]);

  useEffect(() => {
    merchantIdRef.current = merchantId;
  }, [merchantId]);

  useEffect(() => {
    viewRef.current = view;
  }, [view]);

  async function loadHistory(idArg) {
    const id = idArg || merchantIdRef.current;
    if (!id) return;

    try {
      const res = await fetch(`/api/history?merchant_id=${encodeURIComponent(id)}`);
      const data = await res.json();
      if (data.success) {
        setHistory(Array.isArray(data.history) ? data.history : []);
      }
    } catch (err) {
      console.error("Failed to load history", err);
    }
  }

  async function loadAnalytics(idArg) {
    const id = idArg || merchantIdRef.current;
    if (!id) return;

    try {
      const res = await fetch(`/api/analytics?merchant_id=${encodeURIComponent(id)}`);
      const data = await res.json();
      if (data.success) {
        setAnalytics(data);
      }
    } catch (err) {
      console.error("Failed to load analytics", err);
    }
  }

  async function loadAuditLogs(idArg) {
    const id = idArg || merchantIdRef.current;
    if (!id) return;

    try {
      const query = new URLSearchParams({
        merchant_id: id,
        action: auditAction,
        status: auditStatus,
      });
      const res = await fetch(`/api/audit-logs?${query.toString()}`);
      const data = await res.json();
      if (data.success) {
        setAuditLogs(Array.isArray(data.logs) ? data.logs : []);
      }
    } catch (err) {
      console.error("Failed to load audit logs", err);
    }
  }

  function resetFlow() {
    setScreen("amount");
    setAmountStr("");
    setCurrentQrId("");
    setCurrentAmount(0);
    setQrImage("");
    setIsDemoMode(false);
    setQrError("");
    setTimerRemaining(0);
    setTimerTotal(0);
    setResult((prev) => ({ ...prev, open: false }));
    loadHistory();
  }

  function onModeChange(mode) {
    setPaymentMode(mode);
    localStorage.setItem("upiguard_payment_mode", mode);
    setQrError("");
  }

  function handlePaymentResult(data) {
    setTimerRemaining(0);
    setResult({
      open: true,
      status: data.status,
      paid: data.paid || 0,
      expected: data.expected || 0,
      upi_id: data.upi_id || "Unknown",
      transaction_id: data.transaction_id || "",
      fraud_reasons: data.fraud_reasons || [],
    });

    playSound(data.status === STATUS.SUCCESS ? "success" : "fraud");

    if (viewRef.current === "analytics") {
      loadAnalytics();
    }
  }

  useEffect(() => {
    const socket = io();
    socketRef.current = socket;

    socket.on("connect", () => {
      setConnected(true);
      if (merchantIdRef.current) {
        socket.emit("join", { merchant_id: merchantIdRef.current });
      }
    });

    socket.on("disconnect", () => {
      setConnected(false);
    });

    socket.on("payment_result", (data) => {
      handlePaymentResult(data);
    });

    return () => {
      socket.disconnect();
    };
  }, []);

  useEffect(() => {
    if (connected && merchantId && socketRef.current) {
      socketRef.current.emit("join", { merchant_id: merchantId });
    }
  }, [connected, merchantId]);

  useEffect(() => {
    if (merchantId) {
      loadHistory(merchantId);
    }
  }, [merchantId]);

  useEffect(() => {
    if (!merchantId) return;
    if (view === "analytics") {
      loadAnalytics(merchantId);
    }
    if (view === "audit") {
      loadAuditLogs(merchantId);
    }
  }, [view, merchantId]);

  useEffect(() => {
    if (view === "audit" && merchantId) {
      loadAuditLogs(merchantId);
    }
  }, [auditAction, auditStatus]);

  useEffect(() => {
    if (screen !== "qr" || timerRemaining <= 0) {
      return undefined;
    }

    const timer = setInterval(() => {
      setTimerRemaining((prev) => prev - 1);
    }, 1000);

    return () => clearInterval(timer);
  }, [screen, timerRemaining]);

  useEffect(() => {
    if (screen === "qr" && timerRemaining <= 0 && currentQrId) {
      const t = setTimeout(() => {
        resetFlow();
      }, 1200);
      return () => clearTimeout(t);
    }
    return undefined;
  }, [screen, timerRemaining, currentQrId]);

  useEffect(() => {
    if (!result.open) {
      return undefined;
    }

    setResultCountdown(6);
    const timer = setInterval(() => {
      setResultCountdown((prev) => {
        if (prev <= 1) {
          clearInterval(timer);
          resetFlow();
          return 0;
        }
        return prev - 1;
      });
    }, 1000);

    return () => clearInterval(timer);
  }, [result.open]);

  useEffect(() => {
    if (view !== "analytics") {
      return undefined;
    }
    if (!window.Chart) {
      return undefined;
    }
    if (!revenueCanvasRef.current || !hoursCanvasRef.current) {
      return undefined;
    }

    Chart.defaults.color = "#5f6978";
    Chart.defaults.font.family = "'Manrope', sans-serif";
    Chart.defaults.plugins.tooltip.backgroundColor = "#ffffff";
    Chart.defaults.plugins.tooltip.titleColor = "#1d232e";
    Chart.defaults.plugins.tooltip.bodyColor = "#5f6978";
    Chart.defaults.plugins.tooltip.borderColor = "#d8dde6";
    Chart.defaults.plugins.tooltip.borderWidth = 1;

    if (revenueChartRef.current) {
      revenueChartRef.current.destroy();
    }

    if (hoursChartRef.current) {
      hoursChartRef.current.destroy();
    }

    const revCtx = revenueCanvasRef.current.getContext("2d");
    const gradient = revCtx.createLinearGradient(0, 0, 0, 300);
    gradient.addColorStop(0, "rgba(25, 135, 84, 0.5)");
    gradient.addColorStop(1, "rgba(25, 135, 84, 0)");

    revenueChartRef.current = new Chart(revCtx, {
      type: "bar",
      data: {
        labels: analytics.charts.daily_revenue.labels,
        datasets: [
          {
            label: "Revenue",
            data: analytics.charts.daily_revenue.data,
            backgroundColor: gradient,
            borderColor: "rgba(25, 135, 84, 1)",
            borderWidth: 1,
            borderRadius: 4,
            barPercentage: 0.6,
          },
        ],
      },
      options: {
        responsive: true,
        maintainAspectRatio: false,
        plugins: {
          legend: { display: false },
        },
        scales: {
          y: {
            beginAtZero: true,
            grid: { color: "#dbe3ef" },
          },
          x: {
            grid: { display: false },
          },
        },
      },
    });

    const hourCtx = hoursCanvasRef.current.getContext("2d");
    hoursChartRef.current = new Chart(hourCtx, {
      type: "line",
      data: {
        labels: analytics.charts.hourly_distribution.labels,
        datasets: [
          {
            label: "Transactions",
            data: analytics.charts.hourly_distribution.data,
            borderColor: "rgba(217, 95, 58, 1)",
            backgroundColor: "rgba(217, 95, 58, 0.12)",
            borderWidth: 2,
            tension: 0.4,
            fill: true,
            pointRadius: 2.5,
          },
        ],
      },
      options: {
        responsive: true,
        maintainAspectRatio: false,
        plugins: {
          legend: { display: false },
        },
        scales: {
          y: {
            beginAtZero: true,
            ticks: { stepSize: 1 },
            grid: { color: "#dbe3ef" },
          },
          x: {
            grid: { display: false },
          },
        },
      },
    });

    return () => {
      if (revenueChartRef.current) {
        revenueChartRef.current.destroy();
      }
      if (hoursChartRef.current) {
        hoursChartRef.current.destroy();
      }
    };
  }, [view, analytics]);

  async function onGenerateQr() {
    const amount = parseFloat(amountStr);
    if (!amount || amount <= 0 || !merchantId || !merchantName) {
      return;
    }

    setQrError("");
    setCreatingQr(true);

    try {
      const res = await fetch("/api/create-qr", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          amount,
          merchant_id: merchantId,
          merchant_name: merchantName,
          mode: paymentMode,
        }),
      });

      const data = await res.json();
      if (data.success) {
        setCurrentQrId(data.qr_id);
        setCurrentAmount(amount);
        setQrImage(data.image_b64 || data.image_url || "");
        setIsDemoMode(Boolean(data.demo_mode));
        const expiry = Number(data.expires_in) || 300;
        setTimerTotal(expiry);
        setTimerRemaining(expiry);
        setScreen("qr");
      } else {
        setQrError(data.error || "Unable to generate QR right now.");
      }
    } catch (err) {
      console.error("Failed to create QR", err);
      setQrError("Unable to reach server. Please try again.");
    } finally {
      setCreatingQr(false);
    }
  }

  async function simulatePayment(amount) {
    if (!currentQrId) {
      return;
    }

    try {
      const res = await fetch("/api/simulate-payment", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          qr_id: currentQrId,
          amount,
          upi_id: demoUpiId || "demo@upi",
        }),
      });
      const data = await res.json();
      if (data.result) {
        handlePaymentResult(data.result);
      }
    } catch (err) {
      console.error("Failed to simulate payment", err);
    }
  }

  function onNumpad(value) {
    if (value === "back") {
      setAmountStr((prev) => prev.slice(0, -1));
      return;
    }

    if (value === ".") {
      setAmountStr((prev) => {
        if (prev.includes(".")) {
          return prev;
        }
        return prev === "" ? "0." : `${prev}.`;
      });
      return;
    }

    setAmountStr((prev) => {
      const parts = prev.split(".");
      if (parts[0].length >= 6 && !prev.includes(".")) {
        return prev;
      }
      if (parts[1] && parts[1].length >= 2) {
        return prev;
      }
      return `${prev}${value}`;
    });
  }

  function onLogin() {
    const cleanName = loginName.trim();
    if (!cleanName) {
      return;
    }

    const newId = generateMerchantId();
    localStorage.setItem("upiguard_merchant_id", newId);
    localStorage.setItem("upiguard_merchant_name", cleanName);

    setMerchantId(newId);
    setMerchantName(cleanName);
    setLoginName("");
  }

  const parsedAmount = Number.parseFloat(amountStr);
  const validAmount = Boolean(parsedAmount > 0);
  const timerWidth = timerTotal > 0 ? `${Math.max(0, (timerRemaining / timerTotal) * 100)}%` : "0%";

  return (
    <div className="app">
      <header className="topbar">
        <div className="brand">
          <div className="brand-mark">U</div>
          <div className="brand-name">UPI Guard Terminal</div>
          <a className="back-link" href="/">Public Landing</a>
        </div>

        <div className="view-toggle">
          <button className={`view-btn ${view === "terminal" ? "active" : ""}`} onClick={() => setView("terminal")}>Terminal</button>
          <button className={`view-btn ${view === "analytics" ? "active" : ""}`} onClick={() => setView("analytics")}>Analytics</button>
          <button className={`view-btn ${view === "audit" ? "active" : ""}`} onClick={() => setView("audit")}>Audit Trail</button>
        </div>

        <div className="status-pill">
          <span className={`dot ${connected ? "live" : ""}`} />
          {connected ? "Live Session" : "Offline"}
        </div>
      </header>

      <main className="page">
        {view === "terminal" && (
          <section className="terminal-grid">
            <div className="col">
              <div className="card">
                <div className="card-head">
                  <span>Merchant Details</span>
                </div>
                <div className="merchant">
                  <div className="avatar">{merchantAvatar}</div>
                  <div>
                    <h3>
                      {merchantName || "Merchant"}
                      <span className="badge safe">Verified</span>
                    </h3>
                    <p>{merchantVpa}</p>
                  </div>
                </div>
              </div>

              <div className="card">
                <div className="card-head">
                  <span>Recent Transactions</span>
                  <button className="btn-ghost" style={{ width: "auto", marginTop: 0, padding: "7px 12px" }} onClick={() => loadHistory()}>Refresh</button>
                </div>

                <div className="history-list">
                  {history.length === 0 && <div className="muted">No recent activity</div>}

                  {history.slice().reverse().map((txn, idx) => {
                    const isSuccess = txn.status === STATUS.SUCCESS;
                    const timeText = txn.timestamp
                      ? new Date(txn.timestamp * 1000).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })
                      : "";

                    const firstReason = txn.fraud_reasons && txn.fraud_reasons.length > 0 ? txn.fraud_reasons[0] : "";
                    const shortReason = firstReason.length > 30 ? `${firstReason.slice(0, 30)}...` : firstReason;

                    return (
                      <div className="history-item" key={`${txn.transaction_id || "txn"}_${idx}`}>
                        <div className="history-left">
                          <div className="history-amount">Rs {formatAmount(txn.paid)}</div>
                          <div>
                            <div className="history-name">
                              Customer
                              <span className="history-upi">({txn.upi_id || "Unknown"})</span>
                            </div>
                            <div className="history-sub">
                              <span>{txn.transaction_id ? txn.transaction_id.slice(0, 8) : "AUTO"}</span>
                              <span>{timeText}</span>
                              {isSuccess && (
                                <a className="link" target="_blank" rel="noreferrer" href={`/api/receipt/${txn.qr_id || currentQrId}?txn_id=${txn.transaction_id || ""}`}>Receipt</a>
                              )}
                            </div>
                          </div>
                        </div>

                        <div className="history-right">
                          <span className={`badge ${isSuccess ? "safe" : "danger"}`}>
                            {isSuccess ? "Safe" : "Suspicious"}
                          </span>
                          {!isSuccess && shortReason && <div className="small-danger">{shortReason}</div>}
                        </div>
                      </div>
                    );
                  })}
                </div>
              </div>
            </div>

            <div className="col">
              {screen === "amount" && (
                <div className="card">
                  <div className="card-head">
                    <span>Bill Amount</span>
                    <span>...</span>
                  </div>

                  <div className="view-toggle" style={{ marginBottom: 12 }}>
                    <button
                      className={`view-btn ${paymentMode === "live" ? "active" : ""}`}
                      onClick={() => onModeChange("live")}
                    >
                      Live
                    </button>
                    <button
                      className={`view-btn ${paymentMode === "demo" ? "active" : ""}`}
                      onClick={() => onModeChange("demo")}
                    >
                      Demo
                    </button>
                  </div>

                  <div className="muted" style={{ marginBottom: 10 }}>
                    {paymentMode === "live"
                      ? "Live mode: customer scans QR, pays via Cashfree, payment verified automatically."
                      : "Demo mode allows instant simulation for testing."}
                  </div>

                  {qrError && <div className="small-danger" style={{ marginBottom: 10 }}>{qrError}</div>}

                  <div className="display">
                    <span className="currency">Rs</span>
                    <span className={`amount ${!validAmount ? "placeholder" : ""}`}>{validAmount ? amountStr : "0"}</span>
                  </div>

                  <div className="numpad">
                    {["1", "2", "3", "4", "5", "6", "7", "8", "9", ".", "0"].map((key) => (
                      <button className="key" key={key} onClick={() => onNumpad(key)}>{key}</button>
                    ))}
                    <button className="key action" onClick={() => onNumpad("back")}>DEL</button>
                  </div>

                  <button className="btn-main" disabled={!validAmount || creatingQr || !merchantId} onClick={onGenerateQr}>
                    {creatingQr ? "Generating..." : `Proceed to Pay Rs ${validAmount ? amountStr : "0"}`}
                  </button>
                </div>
              )}

              {screen === "qr" && (
                <div className="card">
                  <div className="card-head">
                    <span>Awaiting Payment</span>
                    <span>Rs {formatAmount(currentAmount)}</span>
                  </div>

                  <div className="qr-wrap">
                    <div className="qr-box">
                      {qrImage ? <img src={qrImage} alt="UPI QR" /> : <div className="muted">QR loading</div>}
                    </div>

                    <div className="timer">
                      <div className="timer-bg">
                        <div className="timer-bar" style={{ width: timerWidth }} />
                      </div>
                      <div className="timer-label">Expires in {formatClock(timerRemaining)}</div>
                    </div>
                  </div>

                  {isDemoMode && (
                    <div className="demo-box">
                      <div className="demo-caption">Simulate Demo Payment</div>
                      <input
                        className="demo-input"
                        type="text"
                        value={demoUpiId}
                        onChange={(e) => setDemoUpiId(e.target.value)}
                        placeholder="Customer UPI ID"
                      />
                      <div className="demo-actions">
                        <button className="btn-demo safe" onClick={() => simulatePayment(currentAmount)}>Exact Amount</button>
                        <button className="btn-demo fraud" onClick={() => simulatePayment(1)}>Pay Rs 1</button>
                      </div>
                    </div>
                  )}

                  {!isDemoMode && (
                    <div className="muted" style={{ textAlign: "center", margin: "8px 0" }}>
                      Waiting for payment... verification is automatic.
                    </div>
                  )}

                  <button className="btn-ghost" onClick={resetFlow}>Cancel Transaction</button>
                </div>
              )}
            </div>
          </section>
        )}

        {view === "analytics" && (
          <section>
            <div className="metrics">
              <div className="metric-card">
                <div className="metric-label">TOTAL TRANSACTIONS</div>
                <div className="metric-value">{analytics.summary.total_transactions}</div>
              </div>
              <div className="metric-card">
                <div className="metric-label">SUCCESS RATE</div>
                <div className="metric-value success">{analytics.summary.success_rate}%</div>
              </div>
              <div className="metric-card">
                <div className="metric-label">FRAUD ATTEMPTS BLOCKED</div>
                <div className="metric-value danger">{analytics.summary.fraud_count}</div>
              </div>
            </div>

            <div className="charts">
              <div className="chart-card">
                <div className="chart-title">Revenue (Last 7 Days)</div>
                <div className="chart-holder">
                  <canvas ref={revenueCanvasRef} />
                </div>
              </div>

              <div className="chart-card">
                <div className="chart-title">Peak Payment Hours</div>
                <div className="chart-holder">
                  <canvas ref={hoursCanvasRef} />
                </div>
              </div>
            </div>
          </section>
        )}

        {view === "audit" && (
          <section>
            <div className="filter-bar">
              <select className="filter" value={auditAction} onChange={(e) => setAuditAction(e.target.value)}>
                <option value="ALL">All Actions</option>
                <option value="QR_GENERATED">QR Generated</option>
                <option value="PAYMENT_RECEIVED">Payment Received</option>
                <option value="FRAUD_FLAGGED">Fraud Flagged</option>
              </select>
              <select className="filter" value={auditStatus} onChange={(e) => setAuditStatus(e.target.value)}>
                <option value="ALL">All Statuses</option>
                <option value="SUCCESS">Success</option>
                <option value="SUSPICIOUS">Suspicious</option>
                <option value="INFO">Info</option>
              </select>
              <button className="btn-ghost" style={{ width: "auto", marginTop: 0, padding: "8px 12px" }} onClick={() => loadAuditLogs()}>
                Refresh
              </button>
            </div>

            <div className="table-wrap">
              <table>
                <thead>
                  <tr>
                    <th>Timestamp</th>
                    <th>Action</th>
                    <th>UPI ID</th>
                    <th>Amount</th>
                    <th>Status</th>
                    <th>Details</th>
                  </tr>
                </thead>
                <tbody>
                  {auditLogs.length === 0 && (
                    <tr>
                      <td colSpan="6" className="muted">No audit logs found</td>
                    </tr>
                  )}

                  {auditLogs.map((log, idx) => {
                    const dt = new Date(log.timestamp * 1000);
                    const timeText = `${dt.toLocaleDateString()} ${dt.toLocaleTimeString([], {
                      hour: "2-digit",
                      minute: "2-digit",
                      second: "2-digit",
                    })}`;

                    const statusClass =
                      log.status === "SUCCESS"
                        ? "safe"
                        : log.status === "SUSPICIOUS"
                          ? "danger"
                          : "warn";

                    return (
                      <tr key={`${log.action}_${idx}_${log.timestamp}`}>
                        <td className="muted">{timeText}</td>
                        <td>{log.action}</td>
                        <td className="mono">{log.upi_id || "--"}</td>
                        <td className="mono">Rs {formatAmount(log.amount)}</td>
                        <td>
                          <span className={`badge ${statusClass}`}>{log.status}</span>
                        </td>
                        <td>
                          <button className="btn-ghost" style={{ width: "auto", marginTop: 0, padding: "6px 10px" }} onClick={() => window.alert(JSON.stringify(log.details || {}, null, 2))}>
                            View JSON
                          </button>
                        </td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            </div>
          </section>
        )}
      </main>

      {result.open && (
        <div className="overlay">
          <div className={`result-box ${result.status === STATUS.SUCCESS ? "success" : "fail"}`}>
            <div className={`result-icon ${result.status === STATUS.SUCCESS ? "success" : "fail"}`}>
              {result.status === STATUS.SUCCESS ? "OK" : "!"}
            </div>
            <div className="result-amount">Rs {formatAmount(result.paid)}</div>
            <div className="result-label">
              {result.status === STATUS.SUCCESS ? "Secure Transfer Complete" : "Fraud Alert Detected"}
            </div>

            {result.status !== STATUS.SUCCESS && result.fraud_reasons.length > 0 && (
              <div className="reasons">
                {result.fraud_reasons.map((reason, idx) => (
                  <div className="reason-item" key={`${reason}_${idx}`}>{reason}</div>
                ))}
              </div>
            )}

            <div className="result-detail">
              {result.status === STATUS.SUCCESS
                ? `TXN: ${result.transaction_id || ""} | UPI: ${result.upi_id || "Unknown"}`
                : `Expected Rs ${formatAmount(result.expected)} / Got Rs ${formatAmount(result.paid)} | UPI: ${result.upi_id || "Unknown"}`}
            </div>

            {result.status === STATUS.SUCCESS && (
              <a className="result-receipt" href={`/api/receipt/${currentQrId}?txn_id=${result.transaction_id || ""}`} target="_blank" rel="noreferrer">
                Download PDF Receipt
              </a>
            )}

            <div className="result-count">Resetting in {resultCountdown}s...</div>
          </div>
        </div>
      )}

      {(!merchantId || !merchantName) && (
        <div className="modal">
          <div className="modal-card">
            <h2>Access Terminal</h2>
            <p>Enter your business name to create a secure merchant session.</p>
            <input
              className="modal-input"
              value={loginName}
              onChange={(e) => setLoginName(e.target.value)}
              placeholder="Example: Sharma Kirana Store"
              autoComplete="off"
              onKeyDown={(e) => {
                if (e.key === "Enter") {
                  onLogin();
                }
              }}
            />
            <button className="modal-btn" onClick={onLogin}>Open Terminal</button>
          </div>
        </div>
      )}
    </div>
  );
}

ReactDOM.createRoot(document.getElementById("root")).render(<App />);
