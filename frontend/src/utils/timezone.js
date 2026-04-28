const TIME_ZONE = 'Asia/Shanghai';

const hasExplicitTimezone = (value) => (
  /(?:Z|[+-]\d{2}:?\d{2})$/i.test(value)
);

export const parseApiDate = (value) => {
  if (!value) {
    return null;
  }

  if (value instanceof Date) {
    return Number.isNaN(value.getTime()) ? null : value;
  }

  if (typeof value !== 'string') {
    const date = new Date(value);
    return Number.isNaN(date.getTime()) ? null : date;
  }

  const normalized = hasExplicitTimezone(value) ? value : `${value}Z`;
  const date = new Date(normalized);
  return Number.isNaN(date.getTime()) ? null : date;
};

export const formatChinaDateTime = (value, fallback = '-') => {
  const date = parseApiDate(value);
  if (!date) {
    return fallback;
  }

  return new Intl.DateTimeFormat('zh-CN', {
    timeZone: TIME_ZONE,
    year: 'numeric',
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
    second: '2-digit',
    hour12: false,
  }).format(date);
};

export const formatChinaDate = (value, fallback = '-') => {
  const date = parseApiDate(value);
  if (!date) {
    return fallback;
  }

  return new Intl.DateTimeFormat('zh-CN', {
    timeZone: TIME_ZONE,
    year: 'numeric',
    month: '2-digit',
    day: '2-digit',
  }).format(date);
};

export const formatChinaDateForFilename = (value = new Date()) => {
  const date = parseApiDate(value) || new Date();
  const parts = new Intl.DateTimeFormat('en-CA', {
    timeZone: TIME_ZONE,
    year: 'numeric',
    month: '2-digit',
    day: '2-digit',
  }).formatToParts(date);
  const getPart = (type) => parts.find((part) => part.type === type)?.value;
  return `${getPart('year')}-${getPart('month')}-${getPart('day')}`;
};
