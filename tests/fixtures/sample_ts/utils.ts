export function slugify(value: string): string {
  return normalize(value).replace(/ /g, "-");
}

function normalize(value: string): string {
  return value.trim().toLowerCase();
}
