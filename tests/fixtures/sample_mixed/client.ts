/** TypeScript frontend that calls the backend user API by its shared name. */
export function loadUser(id: string): unknown {
  return getUser(id);
}
