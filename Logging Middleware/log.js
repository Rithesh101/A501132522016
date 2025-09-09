import axios from "axios";

const LOG_API_URL = "http://20.244.56.144/evaluation-service/logs";

async function Log(stack, level, pkg, message) {
  try {
    const body = {
      stack: stack.toLowerCase(),
      level: level.toLowerCase(),
      package: pkg.toLowerCase(),
      message: message
    };

    const response = await axios.post(LOG_API_URL, body, {
      headers: { "Content-Type": "application/json" }
    });

    console.log("Log sent:", response.data);
  } catch (error) {
    console.error("Failed to send log:", error.message);
  }
}

export default Log;

