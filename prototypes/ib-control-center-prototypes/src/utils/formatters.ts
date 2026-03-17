export const formatCurrency = (value: number) =>
  new Intl.NumberFormat("en-US", {
    style: "currency",
    currency: "USD",
    maximumFractionDigits: 2,
  }).format(value);

export const formatSigned = (value: number) => `${value >= 0 ? "+" : ""}${formatCurrency(value)}`;

export const formatTime = (iso: string) =>
  new Intl.DateTimeFormat("en-US", {
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
  }).format(new Date(iso));

export const cx = (...values: Array<string | false | null | undefined>) =>
  values.filter(Boolean).join(" ");
