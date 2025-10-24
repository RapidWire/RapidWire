const API_BASE_URL = "http://127.0.0.1:14540";
let networkDecimals = 6;
function formatAmount(amount) {
    try {
        const val = BigInt(amount);
        const divisor = BigInt(10 ** networkDecimals);
        const integerPart = BigInt(val / divisor).toLocaleString();
        
        const fractionalPart = (val % divisor).toString().padStart(networkDecimals, '0');
        const trimmedFractional = fractionalPart.replace(/0+$/, '');

        if (trimmedFractional === '') {
            return integerPart;
        } else {
            return `${integerPart}.${trimmedFractional}`;
        }
    } catch (e) {
        return 'Invalid Amount';
    }
}
function shortenHex(hexString) {
        if (!hexString || typeof hexString !== 'string' || hexString.length < 12) return hexString;
        if (hexString === '0'.repeat(32)) return '<Strong>System Address</Strong>';
        return `${hexString.substring(0, 6)}...${hexString.substring(hexString.length - 6)}`;
}