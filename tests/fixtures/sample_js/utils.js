const path = require("path");

export function slugify(value) {
  return normalize(value).replace(/ /g, "-");
}

function normalize(value) {
  return value.trim().toLowerCase();
}
