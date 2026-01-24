const API_BASE_URL = "";
let networkDecimals = 3;

const pendingRequests = new Map();

function escapeHtml(text) {
    if (!text) return text;
    return String(text)
        .replace(/&/g, "&amp;")
        .replace(/</g, "&lt;")
        .replace(/>/g, "&gt;")
        .replace(/"/g, "&quot;")
        .replace(/'/g, "&#039;");
}

function parseHugeIntJson(jsonStr) {
    if (!jsonStr) return jsonStr;
    const processed = jsonStr.replace(/("[^"\\]*(?:\\.[^"\\]*)*")|(-?\d{16,})(?![.eE\d])/g, (match, str, num) => {
        if (str) return str;
        return `"${num}"`;
    });
    return JSON.parse(processed);
}

async function fetchWithCache(url, ttl = 60000) { // Default TTL: 60 seconds
    const cachedString = sessionStorage.getItem(url);
    if (cachedString) {
        try {
            const cached = JSON.parse(cachedString);
            if (Date.now() < cached.expiry) {
                return cached.data;
            } else {
                sessionStorage.removeItem(url);
            }
        } catch (e) {
            sessionStorage.removeItem(url);
        }
    }

    if (pendingRequests.has(url)) {
        return await pendingRequests.get(url);
    }

    const promise = fetch(url).then(async (response) => {
        if (!response.ok) {
            throw new Error(`Failed to fetch: ${url} (Status: ${response.status})`);
        }
        const text = await response.text();
        const data = parseHugeIntJson(text);

        const cacheEntry = {
            expiry: Date.now() + ttl,
            data: data
        };
        sessionStorage.setItem(url, JSON.stringify(cacheEntry));

        pendingRequests.delete(url);
        return data;
    }).catch(error => {
        pendingRequests.delete(url);
        throw error;
    });

    pendingRequests.set(url, promise);
    return await promise;
}

async function getConfig() {
    try {
        const config = await fetchWithCache(`${API_BASE_URL}/config`);
        networkDecimals = config.decimal_places;
    } catch (error) {
        console.error("Could not fetch config:", error);
    }
}

async function getUserName(userId) {
    if (userId == 0) return "System Address";
    try {
        const data = await fetchWithCache(`${API_BASE_URL}/user/${userId}/name`);
        return data.username;
    } catch (error) {
        return null;
    }
}

async function formatUserLink(userId) {
    const username = await getUserName(userId);
    const safeUsername = escapeHtml(username);
    const displayName = username ? `👤 ${safeUsername}` : userId;
    const fallbackName = username ? `(${userId})` : '';
    
    return `
        <a href="address.html?user_id=${userId}" class="inline-block group">
            <span class="font-mono text-blue-600 group-hover:underline">${displayName}</span>
            ${fallbackName ? `<span class="font-mono text-xs text-slate-500 ml-1">${fallbackName}</span>` : ''}
        </a>
    `;
}

function formatAmount(amount, decimals = networkDecimals) {
    try {
        let valueToProcess = amount;

        if (typeof valueToProcess === 'string') {
            try {
                valueToProcess = JSON.parse(valueToProcess);
            } catch (e) {
            }
        }

        if (Array.isArray(valueToProcess)) {
            valueToProcess = valueToProcess.join('');
        }

        const val = BigInt(String(valueToProcess));

        const divisor = BigInt(10 ** decimals);
        const integerPart = (val / divisor).toString();
        const fractionalPart = (val % divisor).toString().padStart(decimals, '0').replace(/0+$/, '');

        if (fractionalPart === '') {
            return integerPart;
        } else {
            return `${integerPart}.${fractionalPart}`;
        }
    } catch (e) {
        return 'Invalid Amount';
    }
}

function shortenHex(hexString) {
    if (!hexString || typeof hexString !== 'string' || hexString.length < 12) {
        return hexString;
    }
    if (hexString === '0'.repeat(32)) {
        return '<strong>System Address</strong>';
    }
    return `${hexString.substring(0, 6)}...${hexString.substring(hexString.length - 6)}`;
}
