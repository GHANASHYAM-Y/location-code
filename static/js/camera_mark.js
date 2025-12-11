// static/js/camera_mark.js
// Camera + live recognition for mark_attendance page
// Sends periodic snapshots to server and displays recognized users.

(() => {
  const START_BTN = document.getElementById('startMarkBtn');
  const STOP_BTN = document.getElementById('stopMarkBtn');
  const VIDEO = document.getElementById('markVideo');
  const STATUS = document.getElementById('markStatus');
  const RECOGNIZED_LIST = document.getElementById('recognizedList');

  // Config
  const SNAP_INTERVAL_MS = 3000;        // 3s between snapshots
  const VERIFY_TIMEOUT = 10000;         // geolocation timeout
  const MIN_CONFIDENCE = 0.5;          // optional client-side threshold
  const VERIFY_URL = '/verify_location';      // quick check endpoint
  const RECOGNIZE_URL = '/recognize_face';   // your server endpoint (or /mark_attendance)
  const RADIUS_OUTSIDE_MESSAGE = "You are outside the radius of college, so go to college and mark your attendance.";

  let stream = null;
  let intervalId = null;
  let lastSentAt = 0;

  // Utils
  function setStatus(msg, tone = 'muted') {
    STATUS.textContent = msg;
    STATUS.className = 'mt-2 small text-' + tone;
  }

  function dataURLToBlob(dataURL) {
    const parts = dataURL.split(';base64,');
    const contentType = parts[0].split(':')[1];
    const raw = atob(parts[1]);
    const uInt8Array = new Uint8Array(raw.length);
    for (let i = 0; i < raw.length; ++i) uInt8Array[i] = raw.charCodeAt(i);
    return new Blob([uInt8Array], { type: contentType });
  }

  async function getLocation() {
    return new Promise((resolve, reject) => {
      if (!navigator.geolocation) return reject('Geolocation not supported');
      navigator.geolocation.getCurrentPosition(
        pos => resolve(pos.coords),
        err => reject(err.message || 'Failed to get location'),
        { enableHighAccuracy: true, timeout: VERIFY_TIMEOUT }
      );
    });
  }

  async function serverVerify(lat, lon) {
    const resp = await fetch(VERIFY_URL, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ latitude: lat, longitude: lon })
    });
    // parse JSON even if non-OK (server should return message)
    const json = await resp.json().catch(() => ({}));
    return { ok: resp.ok, json };
  }

  async function startCamera() {
    stream = await navigator.mediaDevices.getUserMedia({ video: { width: 640, height: 480 }, audio: false });
    VIDEO.srcObject = stream;
    VIDEO.play();
  }

  function stopCamera() {
    if (stream) {
      stream.getTracks().forEach(t => t.stop());
      stream = null;
    }
    VIDEO.pause();
    VIDEO.srcObject = null;
  }

  function captureImageDataURL() {
    const w = VIDEO.videoWidth || 640;
    const h = VIDEO.videoHeight || 480;
    const canvas = document.createElement('canvas');
    canvas.width = w;
    canvas.height = h;
    const ctx = canvas.getContext('2d');
    ctx.drawImage(VIDEO, 0, 0, w, h);
    return canvas.toDataURL('image/jpeg', 0.85);
  }

  async function sendSnapshot(lat, lon) {
    // throttle client-side to avoid bursts
    const now = Date.now();
    if (now - lastSentAt < SNAP_INTERVAL_MS - 200) return; // small guard
    lastSentAt = now;

    const dataUrl = captureImageDataURL();
    const blob = dataURLToBlob(dataUrl);
    const form = new FormData();
    form.append('photo', blob, `snap_${now}.jpg`);
    form.append('latitude', lat);
    form.append('longitude', lon);

    setStatus('Uploading snapshot...', 'secondary');

    try {
      const resp = await fetch(RECOGNIZE_URL, { method: 'POST', body: form });
      const json = await resp.json().catch(() => ({}));

      if (!resp.ok) {
        // If server blocked because outside radius, show exact message if present
        if (json && json.message) {
          setStatus(json.message, 'danger');
        } else {
          setStatus('Recognition server returned an error: ' + (json.reason || resp.status), 'danger');
        }
        return;
      }

      // server returned ok; expect recognition payload
      // Example expected response: { success: true, user_id: "123", confidence: 0.83, message: "..."}
      if (json.success) {
        setStatus(`Recognized ${json.user_id} (conf ${Math.round((json.confidence||0)*100)}%)`, 'success');
        addRecognized(json.user_id, json.confidence);
      } else {
        // not recognized
        const msg = json.message || 'Face not recognized';
        setStatus(msg, 'warning');
      }
    } catch (e) {
      setStatus('Network or server error: ' + e.message, 'danger');
    }
  }

  function addRecognized(userId, confidence) {
    // Avoid duplicates in the session list
    const exists = Array.from(RECOGNIZED_LIST.children).some(li => li.dataset.uid === String(userId));
    if (exists) return;
    const li = document.createElement('li');
    li.className = 'list-group-item d-flex justify-content-between align-items-center';
    li.dataset.uid = String(userId);
    li.textContent = `User: ${userId}`;
    const badge = document.createElement('span');
    badge.className = 'badge bg-primary rounded-pill';
    badge.textContent = confidence ? Math.round(confidence * 100) + '%' : '—';
    li.appendChild(badge);
    RECOGNIZED_LIST.prepend(li);
  }

  // start/stop handlers
  START_BTN.addEventListener('click', async () => {
    START_BTN.disabled = true;
    setStatus('Checking location...', 'muted');

    try {
      const coords = await getLocation();

      // check with server
      const { ok, json } = await serverVerify(coords.latitude, coords.longitude);

      // If server returns non-ok, show message and stop
      if (!ok) {
        // If there is message from server use exact wording for outside radius
        if (json && json.message) {
          // ensure the specific requested message is shown if outside
          if (json.reason === 'outside_radius' || (json.message && json.message.toLowerCase().includes('outside'))) {
            setStatus(RADIUS_OUTSIDE_MESSAGE, 'danger');
          } else {
            setStatus(json.message, 'danger');
          }
        } else {
          setStatus(RADIUS_OUTSIDE_MESSAGE, 'danger');
        }
        START_BTN.disabled = false;
        return;
      }

      // ok & json.allowed === true expected
      if (!json.allowed) {
        // server allowed false but returned OK (edge-case)
        setStatus(RADIUS_OUTSIDE_MESSAGE, 'danger');
        START_BTN.disabled = false;
        return;
      }

      // we're inside radius; start camera & interval
      setStatus('Inside college radius — starting camera...', 'info');

      try {
        await startCamera();
      } catch (camErr) {
        setStatus('Camera permission denied or not available: ' + (camErr.message || camErr), 'danger');
        START_BTN.disabled = false;
        return;
      }

      // start periodic snapshotting
      intervalId = setInterval(async () => {
        if (!stream) return;
        await sendSnapshot(coords.latitude, coords.longitude);
      }, SNAP_INTERVAL_MS);

      // also send an immediate first snapshot
      await sendSnapshot(coords.latitude, coords.longitude);

      // UI state
      STOP_BTN.disabled = false;
      setStatus('Live recognition running...', 'success');

    } catch (err) {
      setStatus('Could not get location: ' + (err.message || err), 'danger');
      START_BTN.disabled = false;
      return;
    }
  });

  STOP_BTN.addEventListener('click', () => {
    // shutdown interval & camera
    if (intervalId) {
      clearInterval(intervalId);
      intervalId = null;
    }
    stopCamera();
    setStatus('Stopped.', 'muted');
    START_BTN.disabled = false;
    STOP_BTN.disabled = true;
  });

  // cleanup on page unload
  window.addEventListener('beforeunload', () => {
    if (intervalId) clearInterval(intervalId);
    if (stream) stopCamera();
  });
})();
