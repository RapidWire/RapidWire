const API_BASE_URL = "";
let networkDecimals = 3;

let currencyCache = {};

async function fetchWithCache(url) {
    if (currencyCache[url]) {
        return currencyCache[url];
    }
    const response = await fetch(url);
    if (!response.ok) {
        throw new Error(`Failed to fetch: ${url}`);
    }
    const data = await response.json();
    currencyCache[url] = data;
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

function formatAmount(amount, decimals = networkDecimals) {
    try {
        let valueToProcess = amount;

        // If it's a string, try to parse it as JSON.
        if (typeof valueToProcess === 'string') {
            try {
                valueToProcess = JSON.parse(valueToProcess);
            } catch (e) {
                // Not a JSON string, do nothing and proceed with the original string.
            }
        }

        // If the result is an array, join it into a single string.
        if (Array.isArray(valueToProcess)) {
            valueToProcess = valueToProcess.join('');
        }

        // No matter what, convert the final value to a string before BigInt
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
