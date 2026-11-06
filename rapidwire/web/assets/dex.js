// Helpers shared by the pages that show DEX pools: fee tiers, exact spot prices from amount strings, provider shares and pair currency labels.
import { currencyLink, groupInteger, h } from "/assets/app.js";

const AMOUNT_PATTERN = /^(-?)([0-9]+)(?:\.([0-9]{1,3}))?$/;
const INTEGER_PATTERN = /^-?[0-9]+$/;

// Decimal places of a displayed spot price.
export const PRICE_DECIMALS = 8;

// The fee tier of a pool as a percent string from fee_bps with integer math: 100 -> "1%", 50 -> "0.5%", 10 -> "0.1%", 5 -> "0.05%".
export function feeTierText(feeBps) {
  if (!Number.isSafeInteger(feeBps) || feeBps < 0) {
    return String(feeBps);
  }
  const whole = Math.trunc(feeBps / 100);
  const fraction = String(feeBps % 100).padStart(2, "0").replace(/0+$/, "");
  return fraction === "" ? `${whole}%` : `${whole}.${fraction}%`;
}

// An API amount string as a BigInt of minimal units (0.001); null for anything else.
export function amountUnits(value) {
  const match = typeof value === "string" ? AMOUNT_PATTERN.exec(value) : null;
  if (match === null) {
    return null;
  }
  const units = BigInt(match[2]) * 1000n + BigInt((match[3] || "").padEnd(3, "0"));
  return match[1] === "-" ? -units : units;
}

// The exact quotient numerator / denominator of two non-negative BigInts as a decimal string rounded half up to PRICE_DECIMALS places; null when the denominator is zero.
export function ratioText(numerator, denominator, decimals = PRICE_DECIMALS) {
  if (denominator <= 0n || numerator < 0n) {
    return null;
  }
  const scale = 10n ** BigInt(decimals);
  const scaled = (numerator * scale * 2n + denominator) / (denominator * 2n);
  const integer = scaled / scale;
  const fraction = (scaled % scale).toString().padStart(decimals, "0");
  return `${groupInteger(integer.toString())}.${fraction}`;
}

// The spot prices of a pool from its reserves: {aInB, bInA}, each a decimal string or null when a reserve is zero or unreadable.
export function spotPrices(pool) {
  const a = amountUnits(pool.reserve_a);
  const b = amountUnits(pool.reserve_b);
  if (a === null || b === null || a <= 0n || b <= 0n) {
    return { aInB: null, bInA: null };
  }
  return { aInB: ratioText(b, a), bInA: ratioText(a, b) };
}

// Provider shares of a pool, total_shares - locked_shares, as a string computed with BigInt; null when either is not an integer string.
export function providerShares(pool) {
  if (!INTEGER_PATTERN.test(String(pool.total_shares)) || !INTEGER_PATTERN.test(String(pool.locked_shares))) {
    return null;
  }
  return (BigInt(pool.total_shares) - BigInt(pool.locked_shares)).toString();
}

// A currency link of a pool side, followed by a deleted badge when the currency is deleted.
export function pairCurrency(id, symbol, deleted) {
  return h("span", { class: "pair-currency" }, currencyLink(id, symbol), deleted ? [" ", h("span", { class: "badge badge--failed", title: "この通貨は削除されています" }, "削除済み")] : null);
}

// The pair of a pool as "A / B" with currency links and deleted markers.
export function pairView(pool) {
  return h("span", { class: "pair" }, pairCurrency(pool.currency_a, pool.symbol_a, pool.deleted_a), " / ", pairCurrency(pool.currency_b, pool.symbol_b, pool.deleted_b));
}

// One spot price line: "1 A = <price> B", or a dash when the price is undefined.
export function priceLine(fromSymbol, toSymbol, price) {
  if (price === null) {
    return h("span", { class: "sub" }, `1 ${fromSymbol} = - (リザーブなし)`);
  }
  return h("span", { class: "amount" }, `1 ${fromSymbol} = ${price}`, h("span", { class: "unit" }, toSymbol));
}
