import { Component, type ReactNode } from "react";
export class ViewBoundary extends Component<
  { children: ReactNode },
  { failed: boolean }
> {
  state = { failed: false };
  static getDerivedStateFromError() {
    return { failed: true };
  }
  render() {
    if (this.state.failed)
      return (
        <main className="center-screen">
          <h1>This view couldn’t load.</h1>
          <p>Refresh to load the latest saved state.</p>
          <button onClick={() => window.location.reload()}>Refresh</button>
        </main>
      );
    return this.props.children;
  }
}
