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
  const [token, setToken] = useState(localStorage.getItem("upiguard_token") || "");
  const [merchantId, setMerchantId] = useState(localStorage.getItem("upiguard_merchant_id") || "");
  const [merchantName, setMerchantName] = useState(localStorage.getItem("upiguard_merchant_name") || "");
  const [authMode, setAuthMode] = useState("login");
  const [authName, setAuthName] = useState("");
  const [authEmail, setAuthEmail] = useState("");
  const [authPassword, setAuthPassword] = useState("");
  const [authUpiVpa, setAuthUpiVpa] = useState("");
  const [authError, setAuthError] = useState("");
  const [authLoading, setAuthLoading] = useState(false);

  const [view, setView] = useState("terminal");
  const [connected, setConnected] = useState(false);

  const [amountStr, setAmountStr] = useState("");
  const [screen, setScreen] = useState("amount");

  const [currentQrId, setCurrentQrId] = useState("");
  const [currentAmount, setCurrentAmount] = useState(0);
  const [qrImage, setQrImage] = useState("");
  const [rzpOrderId, setRzpOrderId] = useState("");
  const [rzpKey, setRzpKey] = useState("");
  const [isDemoMode, setIsDemoMode] = useState(false);
  const [currentFlowMode, setCurrentFlowMode] = useState("");
  const [demoUpiId, setDemoUpiId] = useState("demo@upi");
  const [paymentMode, setPaymentMode] = useState("upi_direct");
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
    risk_score: 0,
    ml_verdict: "",
  });
  const [resultCountdown, setResultCountdown] = useState(6);

  const [creatingQr, setCreatingQr] = useState(false);

  const socketRef = useRef(null);
  const merchantIdRef = useRef(merchantId);
  const viewRef = useRef(view);

  const revenueCanvasRef = useRef(null);
  const hoursCanvasRef = useRef(null);
  const donutCanvasRef = useRef(null);
  const revenueChartRef = useRef(null);
  const hoursChartRef = useRef(null);
  const donutChartRef = useRef(null);

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

  function authFetch(url, options = {}) {
    const headers = { ...options.headers };
    if (token) {
      headers["Authorization"] = `Bearer ${token}`;
    }
    if (options.body && typeof options.body === "string" && !headers["Content-Type"]) {
      headers["Content-Type"] = "application/json";
    }
    return fetch(url, { ...options, headers }).then((res) => {
      if (res.status === 401) {
        setToken("");
        setMerchantId("");
        setMerchantName("");
        localStorage.removeItem("upiguard_token");
        localStorage.removeItem("upiguard_merchant_id");
        localStorage.removeItem("upiguard_merchant_name");
      }
      return res;
    });
  }

  async function loadHistory() {
    if (!token) return;
    try {
      const res = await authFetch("/api/history");
      const data = await res.json();
      if (data.success) {
        setHistory(Array.isArray(data.history) ? data.history : []);
      }
    } catch (err) {
      console.error("Failed to load history", err);
    }
  }

  async function loadAnalytics() {
    if (!token) return;
    try {
      const res = await authFetch("/api/analytics");
      const data = await res.json();
      if (data.success) {
        setAnalytics(data);
      }
    } catch (err) {
      console.error("Failed to load analytics", err);
    }
  }

  async function loadAuditLogs() {
    if (!token) return;
    try {
      const query = new URLSearchParams({
        action: auditAction,
        status: auditStatus,
      });
      const res = await authFetch(`/api/audit-logs?${query.toString()}`);
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
    setRzpOrderId("");
    setRzpKey("");
    setIsDemoMode(false);
    setCurrentFlowMode("");
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

  useEffect(() => {
    // Force default to UPI direct for real-payment demo flow.
    localStorage.setItem("upiguard_payment_mode", "upi_direct");
    setPaymentMode("upi_direct");
  }, []);

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
      risk_score: data.risk_score || 0,
      ml_verdict: data.ml_verdict || "",
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
      if (token) {
        socket.emit("join", { token });
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
    if (connected && token && socketRef.current) {
      socketRef.current.emit("join", { token });
    }
  }, [connected, token]);

  useEffect(() => {
    if (token) {
      loadHistory();
    }
  }, [token]);

  useEffect(() => {
    if (!token) return;
    if (view === "terminal" || view === "analytics") {
      loadAnalytics();
    }
    if (view === "terminal" || view === "audit") {
      loadAuditLogs();
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
    if (screen !== "qr" || !currentQrId || isDemoMode) {
      return undefined;
    }

    const poller = setInterval(async () => {
      try {
        await authFetch(`/api/check-payment/${encodeURIComponent(currentQrId)}`);
      } catch (err) {
        console.warn("Payment status poll failed", err);
      }
    }, 3000);

    return () => clearInterval(poller);
  }, [screen, currentQrId, isDemoMode]);

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
    if (view !== "analytics" && view !== "terminal") {
      return undefined;
    }
    if (!window.Chart) {
      return undefined;
    }
    if (view === "analytics" && (!revenueCanvasRef.current || !hoursCanvasRef.current)) {
      return undefined;
    }
    if (view === "terminal" && (!revenueCanvasRef.current || !donutCanvasRef.current)) {
      return undefined;
    }

    Chart.defaults.color = "#5f6978";
    Chart.defaults.font.family = "'Inter', system-ui, sans-serif";
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

    if (hoursCanvasRef.current) {
      const hourCtx = hoursCanvasRef.current.getContext("2d");
      const hourGradient = hourCtx.createLinearGradient(0, 0, 0, 160);
      hourGradient.addColorStop(0, "rgba(168, 85, 247, 0.4)");
      hourGradient.addColorStop(1, "rgba(168, 85, 247, 0)");

      hoursChartRef.current = new Chart(hourCtx, {
        type: "line",
        data: {
          labels: analytics.charts.hourly_distribution.labels,
          datasets: [
            {
              label: "Transactions",
              data: analytics.charts.hourly_distribution.data,
              borderColor: "rgba(168, 85, 247, 1)",
              backgroundColor: hourGradient,
              borderWidth: 2,
              tension: 0.4,
              fill: true,
              pointRadius: 2.5,
              pointBackgroundColor: "rgba(168, 85, 247, 1)"
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
    }

    if (donutChartRef.current) {
      donutChartRef.current.destroy();
    }
    if (donutCanvasRef.current) {
      const donutCtx = donutCanvasRef.current.getContext("2d");
      const successData = analytics.summary.success_rate || 0;
      const fraudData = 100 - successData;

      donutChartRef.current = new Chart(donutCtx, {
        type: "doughnut",
        data: {
          labels: ["Success", "Suspicious"],
          datasets: [
            {
              data: [successData, fraudData],
              backgroundColor: ["rgba(22, 163, 74, 0.8)", "rgba(220, 38, 38, 0.8)"],
              borderWidth: 0,
              hoverOffset: 2,
            },
          ],
        },
        options: {
          responsive: true,
          maintainAspectRatio: false,
          cutout: "75%",
          plugins: {
            legend: { display: false },
          },
        },
      });
    }

    return () => {
      if (revenueChartRef.current) {
        revenueChartRef.current.destroy();
      }
      if (hoursChartRef.current) {
        hoursChartRef.current.destroy();
      }
      if (donutChartRef.current) {
        donutChartRef.current.destroy();
      }
    };
  }, [view, analytics]);

  async function onGenerateQr() {
    const amount = parseFloat(amountStr);
    if (!amount || amount <= 0 || !token) {
      return;
    }

    setQrError("");
    setCreatingQr(true);

    try {
      const endpoint = paymentMode === "mock" ? "/api/orders" : "/api/create-qr";
      const payload =
        paymentMode === "mock"
          ? { amount }
          : { amount, mode: paymentMode };

      const res = await authFetch(endpoint, {
        method: "POST",
        body: JSON.stringify(payload),
      });

      const data = await res.json();
      if (data.success) {
        setCurrentQrId(data.qr_id);
        setCurrentAmount(amount);
        setQrImage(data.image_b64 || data.image_url || "");
        setRzpOrderId(data.razorpay_order_id || "");
        setRzpKey(data.razorpay_key || "");
        setIsDemoMode(Boolean(data.demo_mode));
        setCurrentFlowMode(data.mode || paymentMode);
        const expiry = Number(data.expires_in) || 300;
        setTimerTotal(expiry);
        setTimerRemaining(expiry);
        setScreen("qr");

        if ((data.mode || paymentMode) === "razorpay_test" && window.Razorpay && data.razorpay_order_id && data.razorpay_key) {
          const rzp = new window.Razorpay({
            key: data.razorpay_key,
            amount: Math.round(amount * 100),
            currency: "INR",
            name: "UPI Guard Demo",
            description: "Razorpay Test Verification",
            order_id: data.razorpay_order_id,
            handler: function () {
              // Backend polling/webhook will finalize status
            },
            modal: {
              ondismiss: function () {},
            },
          });
          rzp.open();
        }
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
      const endpoint = currentFlowMode === "mock" ? "/mock-gateway/pay" : "/api/simulate-payment";
      const payload =
        currentFlowMode === "mock"
          ? {
              order_id: currentQrId,
              paid_amount: amount,
              upi_id: demoUpiId || "demo@upi",
            }
          : {
              qr_id: currentQrId,
              amount,
              upi_id: demoUpiId || "demo@upi",
            };

      const res = await authFetch(endpoint, {
        method: "POST",
        body: JSON.stringify(payload),
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

  async function onAuthSubmit() {
    setAuthError("");
    setAuthLoading(true);
    try {
      const endpoint = authMode === "register" ? "/api/auth/register" : "/api/auth/login";
      const body = authMode === "register"
        ? { name: authName, email: authEmail, password: authPassword, upi_vpa: authUpiVpa }
        : { email: authEmail, password: authPassword };

      const res = await fetch(endpoint, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      });
      const data = await res.json();

      if (!res.ok || !data.success) {
        setAuthError(data.error || "Authentication failed");
        return;
      }

      localStorage.setItem("upiguard_token", data.token);
      localStorage.setItem("upiguard_merchant_id", data.merchant_id);
      localStorage.setItem("upiguard_merchant_name", data.merchant_name);

      setToken(data.token);
      setMerchantId(data.merchant_id);
      setMerchantName(data.merchant_name);
      setAuthName("");
      setAuthEmail("");
      setAuthPassword("");
      setAuthUpiVpa("");
    } catch (err) {
      setAuthError("Could not reach server");
    } finally {
      setAuthLoading(false);
    }
  }

  function onLogout() {
    localStorage.removeItem("upiguard_token");
    localStorage.removeItem("upiguard_merchant_id");
    localStorage.removeItem("upiguard_merchant_name");
    setToken("");
    setMerchantId("");
    setMerchantName("");
    if (socketRef.current) {
      socketRef.current.disconnect();
      socketRef.current.connect();
    }
  }

  const parsedAmount = Number.parseFloat(amountStr);
  const validAmount = Boolean(parsedAmount > 0);
  const timerWidth = timerTotal > 0 ? `${Math.max(0, (timerRemaining / timerTotal) * 100)}%` : "0%";

  return (
    <div className="app">
      <main className="page">
        <div className="dashboard-shell">
          <header className="topbar">
            <div className="brand">
              <div className="brand-mark">U</div>
              <div className="brand-block">
                <div className="brand-name">UPI Guard</div>
              </div>
            </div>

            <div className="top-actions">
              <div className="profile-chip" title={merchantName}>{merchantAvatar || "AT"}</div>
              <div className="status-pill">
                <span className={`dot ${connected ? "live" : ""}`} />
                {connected ? "Connected" : "Offline"}
              </div>
              <button onClick={onLogout} style={{background: "none", border: "1px solid var(--card-border)", borderRadius: 8, padding: "6px 10px", fontSize: 12, fontWeight: 600, color: "var(--ink-soft)", cursor: "pointer"}}>Logout</button>
            </div>
          </header>

          <div className="shell-body">
            <aside className="side-nav">
              <div className="side-group">
                <div className="side-title">Main</div>
                <button className={`side-link ${view === "terminal" ? "active" : ""}`} onClick={() => setView("terminal")}>Home</button>
                <button className={`side-link ${view === "analytics" ? "active" : ""}`} onClick={() => setView("analytics")}>Transactions</button>
                <button className={`side-link ${view === "audit" ? "active" : ""}`} onClick={() => setView("audit")}>Audit Logs</button>
              </div>
            </aside>

            <section className="dashboard-content">
        {view === "terminal" && (
          <section className="terminal-grid">
            <div className="col charts-col">
              <div className="metrics-row">
                <div className="metric-box">
                  <div className="metric-label">Total Transactions</div>
                  <div className="metric-value">{analytics.summary.total_transactions}</div>
                </div>
                <div className="metric-box">
                  <div className="metric-label">Success Rate</div>
                  <div className="metric-value success">{analytics.summary.success_rate}%</div>
                </div>
                <div className="metric-box">
                  <div className="metric-label">Fraud Prevented</div>
                  <div className="metric-value danger">{analytics.summary.fraud_count}</div>
                </div>
              </div>

              <div className="charts-row">
                <div className="chart-card mini">
                  <div className="chart-title">Revenue Trends</div>
                  <div className="chart-holder-small">
                    <canvas ref={revenueCanvasRef} />
                  </div>
                </div>
                <div className="chart-card mini">
                  <div className="chart-title">Success Overview</div>
                  <div className="chart-holder-small">
                    <canvas ref={donutCanvasRef} />
                  </div>
                </div>
              </div>

              <div className="charts-row">
                <div className="chart-card mini">
                  <div className="chart-title">Market Trends</div>
                  <div className="chart-holder-small">
                    <canvas ref={hoursCanvasRef} />
                  </div>
                </div>
                <div className="chart-card mini" style={{ display: 'flex', flexDirection: 'column' }}>
                  <div className="chart-title">Audit Highlights</div>
                  <div className="progress-list" style={{ flex: 1, justifyContent: 'center' }}>
                    {(() => {
                      const stats = { create: 0, success: 0, failed: 0, fraud: 0 };
                      auditLogs.forEach(log => {
                        const action = (log.action || "").toUpperCase();
                        if (action.includes("CREATE")) stats.create++;
                        else if (action.includes("SUCCESS")) stats.success++;
                        else if (action.includes("FAIL") || action.includes("ERROR")) stats.failed++;
                        else if (action.includes("FRAUD") || action.includes("RISK")) stats.fraud++;
                      });
                      const total = auditLogs.length || 1;
                      return (
                        <>
                          <div className="progress-item">
                            <div className="progress-label-row">
                              <span>Payment Success</span>
                              <span>{stats.success}</span>
                            </div>
                            <div className="progress-bar-bg">
                              <div className="progress-bar-fill" style={{ width: `${Math.min(100, (stats.success/total)*100)}%`, background: "var(--green)" }} />
                            </div>
                          </div>
                          <div className="progress-item">
                            <div className="progress-label-row">
                              <span>QR Generated</span>
                              <span>{stats.create}</span>
                            </div>
                            <div className="progress-bar-bg">
                              <div className="progress-bar-fill" style={{ width: `${Math.min(100, (stats.create/total)*100)}%`, background: "var(--accent)" }} />
                            </div>
                          </div>
                          <div className="progress-item">
                            <div className="progress-label-row">
                              <span>Fraud Blocks</span>
                              <span>{stats.fraud}</span>
                            </div>
                            <div className="progress-bar-bg">
                              <div className="progress-bar-fill" style={{ width: `${Math.min(100, (stats.fraud/total)*100)}%`, background: "var(--red)" }} />
                            </div>
                          </div>
                          <div className="progress-item">
                            <div className="progress-label-row">
                              <span>Errors/Fails</span>
                              <span>{stats.failed}</span>
                            </div>
                            <div className="progress-bar-bg">
                              <div className="progress-bar-fill" style={{ width: `${Math.min(100, (stats.failed/total)*100)}%`, background: "var(--yellow)" }} />
                            </div>
                          </div>
                        </>
                      );
                    })()}
                  </div>
                </div>
              </div>

              <div className="card history-wrap">
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
                          {txn.risk_score > 0 && (
                            <span style={{
                              fontSize: "10px",
                              fontWeight: "700",
                              padding: "2px 6px",
                              borderRadius: "999px",
                              marginLeft: "4px",
                              background: txn.risk_score >= 70 ? "rgba(220,38,38,0.1)" : txn.risk_score >= 40 ? "rgba(202,138,4,0.1)" : "rgba(22,163,74,0.1)",
                              color: txn.risk_score >= 70 ? "#dc2626" : txn.risk_score >= 40 ? "#ca8a04" : "#16a34a",
                            }}>
                              ML: {txn.risk_score}
                            </span>
                          )}
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
                    <span>UPI Direct</span>
                  </div>

                  <div className="muted" style={{ marginBottom: 10 }}>
                    Real payment to your UPI ID via deep-link QR.
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

                  {(isDemoMode || currentFlowMode === "mock") && (
                    <div className="demo-box">
                      <div className="demo-caption">{currentFlowMode === "mock" ? "Simulate Mock Gateway Payment" : "Simulate Demo Payment"}</div>
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

                  {currentFlowMode === "razorpay_test" && rzpOrderId && rzpKey && (
                    <button
                      className="btn-main"
                      onClick={() => {
                        if (!window.Razorpay) return;
                        const rzp = new window.Razorpay({
                          key: rzpKey,
                          amount: Math.round(currentAmount * 100),
                          currency: "INR",
                          name: "UPI Guard Demo",
                          description: "Razorpay Test Verification",
                          order_id: rzpOrderId,
                          handler: function () {},
                        });
                        rzp.open();
                      }}
                    >
                      Open Razorpay Checkout
                    </button>
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
            </section>
          </div>
        </div>
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
            {result.risk_score > 0 && (
              <div style={{
                display: "inline-flex",
                alignItems: "center",
                gap: "6px",
                margin: "8px 0",
                padding: "4px 12px",
                borderRadius: "999px",
                fontSize: "12px",
                fontWeight: "700",
                background: result.risk_score >= 70 ? "rgba(220,38,38,0.12)" : result.risk_score >= 40 ? "rgba(202,138,4,0.12)" : "rgba(22,163,74,0.12)",
                color: result.risk_score >= 70 ? "#dc2626" : result.risk_score >= 40 ? "#ca8a04" : "#16a34a",
                border: `1px solid ${result.risk_score >= 70 ? "rgba(220,38,38,0.25)" : result.risk_score >= 40 ? "rgba(202,138,4,0.25)" : "rgba(22,163,74,0.25)"}`,
              }}>
                <span>ML Risk Score: {result.risk_score}/100</span>
                <span style={{ opacity: 0.7 }}>|</span>
                <span>{result.ml_verdict?.toUpperCase()}</span>
              </div>
            )}
            {result.status !== STATUS.SUCCESS && result.fraud_reasons.length > 0 && (
              <div className="reasons">
                {result.fraud_reasons.map((reason, idx) => (
                  <div className="reason-item" key={`reason_${idx}`}>{reason}</div>
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
      {(!token) && (
        <div className="modal">
          <div className="modal-card">
            <h2>{authMode === "register" ? "Create Account" : "Welcome Back"}</h2>
            <p>{authMode === "register" ? "Register to start accepting payments." : "Login to your merchant account."}</p>
            {authMode === "register" && (
              <input className="modal-input" value={authName} onChange={(e) => setAuthName(e.target.value)} placeholder="Business Name" autoComplete="off" style={{marginBottom: 10}} />
            )}
            {authMode === "register" && (
              <input className="modal-input" value={authUpiVpa} onChange={(e) => setAuthUpiVpa(e.target.value)} placeholder="UPI ID (e.g. yourname@okbi)" autoComplete="off" style={{marginBottom: 10}} />
            )}
            <input className="modal-input" value={authEmail} onChange={(e) => setAuthEmail(e.target.value)} placeholder="Email" type="email" autoComplete="off" style={{marginBottom: 10}} />
            <input className="modal-input" value={authPassword} onChange={(e) => setAuthPassword(e.target.value)} placeholder="Password (min 6 chars)" type="password" autoComplete="off" onKeyDown={(e) => { if (e.key === "Enter") onAuthSubmit(); }} />
            {authError && <div style={{color: "#dc2626", fontSize: 13, marginTop: 8, textAlign: "center"}}>{authError}</div>}
            <button className="modal-btn" onClick={onAuthSubmit} disabled={authLoading} style={{marginTop: 14}}>
              {authLoading ? "Please wait..." : authMode === "register" ? "Register" : "Login"}
            </button>
            <div style={{textAlign: "center", marginTop: 12}}>
              <button onClick={() => { setAuthMode(authMode === "register" ? "login" : "register"); setAuthError(""); }} style={{background: "none", border: "none", color: "#3b82f6", cursor: "pointer", fontSize: 13, fontWeight: 600}}>
                {authMode === "register" ? "Already have an account? Login" : "New here? Register"}
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}

ReactDOM.createRoot(document.getElementById("root")).render(<App />);