import { useState, useEffect, useRef } from 'react';

function App() {
  const [logs, setLogs] = useState<string[]>([]);
  const [status, setStatus] = useState<string | null>(null);
  const [scrapeUrl, setScrapeUrl] = useState('');
  const [addGroupUrl, setAddGroupUrl] = useState('');
  const [clearChoice, setClearChoice] = useState('1');
  const logsEndRef = useRef<HTMLDivElement>(null);
  const wsRef = useRef<WebSocket | null>(null);

  // Accounts state
  const [accounts, setAccounts] = useState<{ phone: string, restricted: boolean }[]>([]);
  const [activeAccount, setActiveAccount] = useState<string>('');
  const [isModalOpen, setIsModalOpen] = useState(false);
  const [authStep, setAuthStep] = useState<number>(0); // 0 = list, 1 = phone, 2 = code
  const [newPhone, setNewPhone] = useState('');
  const [authCode, setAuthCode] = useState('');
  const [isAuthLoading, setIsAuthLoading] = useState(false);

  const API_URL = import.meta.env.DEV ? 'http://localhost:8000/api' : '/api';
  const WS_URL = import.meta.env.DEV ? 'ws://localhost:8000/ws/logs' : `${window.location.protocol === 'https:' ? 'wss:' : 'ws:'}//${window.location.host}/ws/logs`;

  const fetchAccounts = async () => {
    try {
      const res = await fetch(`${API_URL}/accounts`);
      if (res.ok) {
        const data = await res.json();
        setAccounts(data.accounts || []);
        setActiveAccount(data.active || '');
      }
    } catch (e) {
      console.error(e);
    }
  };

  useEffect(() => {
    const connectWs = () => {
      const ws = new WebSocket(WS_URL);
      ws.onmessage = (event) => {
        setLogs(prev => [...prev.slice(-999), event.data]);
      };
      ws.onclose = () => {
        setTimeout(connectWs, 3000);
      };
      wsRef.current = ws;
    };
    connectWs();

    fetchAccounts();

    const interval = setInterval(() => {
      fetch(`${API_URL}/status`)
        .then(res => res.json())
        .then(data => {
          setStatus(data.active_task);
        })
        .catch(() => setStatus('offline'));
    }, 2000);

    return () => {
      wsRef.current?.close();
      clearInterval(interval);
    };
  }, []);

  useEffect(() => {
    logsEndRef.current?.scrollIntoView({ behavior: 'smooth' });
  }, [logs]);

  const apiCall = async (endpoint: string, method: string = 'POST', body?: any) => {
    try {
      const res = await fetch(`${API_URL}${endpoint}`, {
        method,
        headers: { 'Content-Type': 'application/json' },
        body: body ? JSON.stringify(body) : undefined
      });
      const data = await res.json();
      if (!res.ok) alert(data.detail || 'Error executing action');
      return { ok: res.ok, data };
    } catch (e) {
      alert('Network Error. Is the backend running?');
      return { ok: false, data: null };
    }
  };

  const isBusy = status !== null && status !== 'offline';

  const handleSwitchAccount = async (phone: string) => {
    setIsAuthLoading(true);
    await apiCall('/accounts/switch', 'POST', { phone });
    await fetchAccounts();
    setIsAuthLoading(false);
    setIsModalOpen(false);
  };

  const handleDeleteAccount = async (phone: string) => {
    if (window.confirm(`Are you sure you want to completely delete the session file for ${phone}?`)) {
      setIsAuthLoading(true);
      await apiCall('/accounts/delete', 'POST', { phone });
      await fetchAccounts();
      setIsAuthLoading(false);
    }
  };

  const handleSendCode = async () => {
    if (!newPhone) return;
    setIsAuthLoading(true);
    const result = await apiCall('/auth/send-code', 'POST', { phone: newPhone });
    setIsAuthLoading(false);

    if (result.ok) {
      if (result.data.status === 'already_authorized') {
        alert('Account is already authorized!');
        setAuthStep(0);
        await fetchAccounts();
      } else {
        setAuthStep(2);
      }
    }
  };

  const handleSubmitCode = async () => {
    if (!authCode) return;
    setIsAuthLoading(true);
    const result = await apiCall('/auth/submit-code', 'POST', { phone: newPhone, code: authCode });
    setIsAuthLoading(false);

    if (result.ok) {
      setAuthStep(0);
      setNewPhone('');
      setAuthCode('');
      setIsModalOpen(false);
      await fetchAccounts();
    }
  };

  return (
    <div className="app-container">
      <header className="header">
        <div className="header-title">TeleBoosterPro</div>
        <div style={{ display: 'flex', alignItems: 'center', gap: '1.5rem' }}>
          <div style={{ display: 'flex', alignItems: 'center', gap: '0.5rem' }}>
            <span className={`status-indicator ${status === 'offline' ? 'error' : isBusy ? 'running' : ''}`} />
            <span style={{ fontWeight: 600 }}>
              {status === 'offline' ? 'Offline' : isBusy ? `Running: ${status}` : 'Idle'}
            </span>
          </div>

          <div
            className="avatar"
            title="Manage Accounts"
            onClick={() => {
              fetchAccounts();
              setAuthStep(0);
              setIsModalOpen(true);
            }}
          >
            {activeAccount ? activeAccount.slice(0, 2).toUpperCase() : '?'}
          </div>
        </div>
      </header>

      {/* Account Management Modal */}
      {isModalOpen && (
        <div className="modal-overlay" onClick={() => setIsModalOpen(false)}>
          <div className="modal" onClick={e => e.stopPropagation()}>
            <div className="modal-header">
              <h3 style={{ margin: 0 }}>
                {authStep === 0 ? 'Manage Accounts' : authStep === 1 ? 'Add New Account' : 'Verification Code'}
              </h3>
              <button className="close-btn" onClick={() => setIsModalOpen(false)}>&times;</button>
            </div>

            {authStep === 0 && (
              <div style={{ display: 'flex', flexDirection: 'column', gap: '1rem' }}>
                <div style={{ maxHeight: '200px', overflowY: 'auto', display: 'flex', flexDirection: 'column', gap: '0.5rem' }}>
                  {accounts.length === 0 && <span style={{ color: 'var(--text-secondary)' }}>No saved accounts</span>}
                  {accounts.map(accData => {
                    const acc = accData.phone;
                    return (
                      <div key={acc} className={`account-item ${acc === activeAccount ? 'active' : ''}`}>
                        <span style={{ fontFamily: 'monospace', fontWeight: 600, display: 'flex', alignItems: 'center' }}>
                          {acc}
                          {accData.restricted && <span style={{ color: '#ef4444', marginLeft: '6px', fontSize: '0.8em' }} title="Temporal Limit (PeerFlood)">🔴</span>}
                        </span>
                        {acc !== activeAccount ? (
                          <div style={{ display: 'flex', gap: '0.5rem' }}>
                            <button
                              className="btn"
                              style={{ padding: '0.25rem 0.75rem', fontSize: '0.8rem' }}
                              onClick={() => handleSwitchAccount(acc)}
                              disabled={isAuthLoading || isBusy}
                            >
                              Switch
                            </button>
                            <button
                              className="btn danger"
                              style={{ padding: '0.25rem 0.5rem', fontSize: '0.8rem' }}
                              onClick={() => handleDeleteAccount(acc)}
                              disabled={isAuthLoading || isBusy}
                              title="Delete this broken session"
                            >
                              ✕
                            </button>
                          </div>
                        ) : (
                          <span className="badge" style={{ background: 'var(--success-color)', color: 'white' }}>Active</span>
                        )}
                      </div>
                    )
                  })}
                </div>

                <button
                  className="btn"
                  style={{ background: 'var(--surface-color)', border: '1px solid var(--border-color)', width: '100%' }}
                  onClick={() => setAuthStep(1)}
                  disabled={isAuthLoading || isBusy}
                >
                  + Add New Account
                </button>
              </div>
            )}

            {authStep === 1 && (
              <div style={{ display: 'flex', flexDirection: 'column', gap: '1rem' }}>
                <p style={{ fontSize: '0.9rem', color: 'var(--text-secondary)' }}>Enter your phone number with country code to add a new session.</p>
                <input
                  className="input-field"
                  placeholder="e.g. +1234567890"
                  value={newPhone}
                  onChange={e => setNewPhone(e.target.value)}
                />
                <div style={{ display: 'flex', gap: '1rem' }}>
                  <button className="btn" style={{ flex: 1, background: 'transparent', border: '1px solid var(--border-color)' }} onClick={() => setAuthStep(0)}>Cancel</button>
                  <button className="btn" style={{ flex: 1 }} onClick={handleSendCode} disabled={isAuthLoading || !newPhone}>
                    {isAuthLoading ? 'Sending...' : 'Send Code'}
                  </button>
                </div>
              </div>
            )}

            {authStep === 2 && (
              <div style={{ display: 'flex', flexDirection: 'column', gap: '1rem' }}>
                <p style={{ fontSize: '0.9rem', color: 'var(--text-secondary)' }}>Enter the verification code sent to {newPhone}.</p>
                <input
                  className="input-field"
                  placeholder="Code"
                  value={authCode}
                  onChange={e => setAuthCode(e.target.value)}
                />
                <div style={{ display: 'flex', gap: '1rem' }}>
                  <button className="btn" style={{ flex: 1, background: 'transparent', border: '1px solid var(--border-color)' }} onClick={() => setAuthStep(1)}>Cancel</button>
                  <button className="btn" style={{ flex: 1 }} onClick={handleSubmitCode} disabled={isAuthLoading || !authCode}>
                    {isAuthLoading ? 'Verifying...' : 'Submit'}
                  </button>
                </div>
              </div>
            )}
          </div>
        </div>
      )}

      <div className="main-content">
        <div className="grid-layout">

          <div className="card">
            <h2 className="card-title">1. Scrape Users</h2>
            <p style={{ fontSize: '0.9rem', color: 'var(--text-secondary)' }}>
              Scrape active users from a target group and save to CSV.
            </p>
            <input
              className="input-field"
              placeholder="e.g. https://t.me/PublicGroup"
              value={scrapeUrl}
              onChange={e => setScrapeUrl(e.target.value)}
            />
            <button
              className="btn"
              onClick={() => apiCall('/scrape', 'POST', { url: scrapeUrl })}
              disabled={isBusy}
            >
              Start Scraping
            </button>
          </div>

          <div className="card">
            <h2 className="card-title">2. Add to Group</h2>
            <p style={{ fontSize: '0.9rem', color: 'var(--text-secondary)' }}>
              Add scraped users incrementally to your target group.
            </p>
            <input
              className="input-field"
              placeholder="e.g. https://t.me/MyGroup"
              value={addGroupUrl}
              onChange={e => setAddGroupUrl(e.target.value)}
            />
            <button
              className="btn"
              onClick={() => apiCall('/add-group', 'POST', { url: addGroupUrl })}
              disabled={isBusy}
            >
              Start Adding
            </button>
          </div>

          <div className="card">
            <h2 className="card-title">3. Add to Contacts</h2>
            <p style={{ fontSize: '0.9rem', color: 'var(--text-secondary)' }}>
              Add scraped users directly to your Telegram contacts safely.
            </p>
            <button
              className="btn success"
              onClick={() => apiCall('/add-contacts')}
              disabled={isBusy}
            >
              Start Adding to Contacts
            </button>
          </div>

          <div className="card">
            <h2 className="card-title">4. Clear Data & History</h2>
            <select className="input-field" value={clearChoice} onChange={e => setClearChoice(e.target.value)}>
              <option value="1">Wipe ALL Data (CSV + History)</option>
              <option value="2">Delete ONLY scraped_users.csv</option>
              <option value="3">Clean CSV (Remove processed users)</option>
            </select>
            <button
              className="btn danger"
              onClick={() => {
                if (window.confirm('Are you sure you want to clear this data?')) {
                  apiCall('/clear', 'POST', { choice: clearChoice });
                }
              }}
              disabled={isBusy}
            >
              Clear Data
            </button>
          </div>

          <div className="card">
            <h2 className="card-title">5. Dangerous Actions</h2>
            <p style={{ fontSize: '0.9rem', color: 'var(--text-secondary)' }}>
              Manage your Telegram session directly.
            </p>
            <div style={{ display: 'flex', gap: '1rem', flexWrap: 'wrap' }}>
              <button
                className="btn danger"
                onClick={() => {
                  if (window.confirm('Wipe ALL Telegram contacts to ZERO? Cannot be undone!')) {
                    apiCall('/clear-contacts');
                  }
                }}
                disabled={isBusy}
              >
                Clear Contacts
              </button>
              <button
                className="btn"
                style={{ background: '#475569', opacity: (!activeAccount || isBusy) ? 0.5 : 1 }}
                onClick={() => {
                  if (window.confirm('Are you sure you want to log out the current account?')) {
                    apiCall('/logout').then(() => fetchAccounts());
                  }
                }}
                disabled={isBusy || !activeAccount}
              >
                Logout Account
              </button>
            </div>
          </div>

        </div>

        <div className="terminal-container">
          <div className="terminal-header">
            <span>Terminal Output</span>
            <div style={{ display: 'flex', gap: '0.5rem', alignItems: 'center' }}>
              {isBusy && (
                <button
                  style={{ background: '#dc2626', color: 'white', border: 'none', borderRadius: '4px', padding: '2px 8px', fontSize: '0.75rem', cursor: 'pointer', fontWeight: 'bold' }}
                  onClick={() => apiCall('/stop')}
                >
                  STOP TASK
                </button>
              )}
              <span className="badge">Live</span>
            </div>
          </div>
          <div className="terminal-body">
            {logs.length === 0 ? (
              <span style={{ color: '#666' }}>Waiting for connection...</span>
            ) : null}
            {logs.map((log, i) => (
              <div key={i}>{log}</div>
            ))}
            <div ref={logsEndRef} />
          </div>
        </div>
      </div>
    </div>
  );
}

export default App;
