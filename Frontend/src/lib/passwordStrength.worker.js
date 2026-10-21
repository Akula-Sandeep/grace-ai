import zxcvbn from 'zxcvbn';

self.onmessage = (event) => {
  const { password, userInputs } = event.data;
  self.postMessage(zxcvbn(password, userInputs).score);
};
