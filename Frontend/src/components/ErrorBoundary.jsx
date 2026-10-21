import { Component } from 'react';
import { captureException } from '../lib/observability';

class ErrorBoundary extends Component {
  constructor(props) {
    super(props);
    this.state = { error: null };
  }

  static getDerivedStateFromError(error) {
    return { error };
  }

  componentDidCatch(error, info) {
    // Surface it in the console at minimum; wire this into a real error
    // reporting service (Sentry, etc.) before shipping to production.
    console.error('Unhandled UI error.');
    void captureException(error, { extra: { componentStack: info?.componentStack } });
  }

  handleReload = () => {
    this.setState({ error: null });
    window.location.reload();
  };

  render() {
    if (this.state.error) {
      return (
        <div className="error-boundary">
          <h1>Something went wrong.</h1>
          <p>
            An unexpected error interrupted the app. Reloading usually fixes this;
            if it keeps happening, please let us know what you were doing.
          </p>
          <button className="error-boundary-button" onClick={this.handleReload}>
            Reload
          </button>
        </div>
      );
    }
    return this.props.children;
  }
}

export default ErrorBoundary;
