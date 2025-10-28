const API_BASE_URL = "";
let networkDecimals = 3;

async function fetchWithCache(url) {
    const cached = sessionStorage.getItem(url);
    if (cached) {
        return JSON.parse(cached);
    }

    const response = await fetch(url);
    if (!response.ok) {
        throw new Error(`Failed to fetch: ${url} (Status: ${response.status})`);
    }
    const data = await response.json();
    sessionStorage.setItem(url, JSON.stringify(data));
    return data;
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
    const displayName = username ? `👤 ${username}` : userId;
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
