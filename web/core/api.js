export async function api(path, options = {}) {
  const response = await fetch(path, options);
  let value = {};
  try { value = await response.json(); } catch (_) { /* empty response */ }
  if (!response.ok) {
    const error = new Error(value.error || `请求失败 (${response.status})`);
    error.status = response.status;
    error.jobId = value.job_id;
    throw error;
  }
  return value;
}

export function jsonPost(value) {
  return {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(value),
  };
}
