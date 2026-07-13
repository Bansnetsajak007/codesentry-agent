import { User } from "./models";

/** Render a greeting for a user. */
export const Greeting = (user: User): JSX.Element => {
  return <div className="greeting">{user.displayName()}</div>;
};
