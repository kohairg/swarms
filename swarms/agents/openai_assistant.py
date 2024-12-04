from typing import Optional, List, Dict, Any, Callable
import time
from openai import OpenAI
from swarms.structs.agent import Agent
import json

class OpenAIAssistant(Agent):
    """
    OpenAI Assistant wrapper for the swarms framework.
    Integrates OpenAI's Assistants API with the swarms architecture.
    
    Example:
        >>> assistant = OpenAIAssistant(
        ...     name="Math Tutor",
        ...     instructions="You are a personal math tutor.",
        ...     model="gpt-4o",
        ...     tools=[{"type": "code_interpreter"}]
        ... )
        >>> response = assistant.run("Solve 3x + 11 = 14")
    """

    def __init__(
        self,
        name: str,
        instructions: Optional[str] = None,
        model: str = "gpt-4o",
        tools: Optional[List[Dict[str, Any]]] = None,
        file_ids: Optional[List[str]] = None,
        metadata: Optional[Dict[str, Any]] = None,
        functions: Optional[List[Dict[str, Any]]] = None,
        *args,
        **kwargs
    ):
        """Initialize the OpenAI Assistant.

        Args:
            name: Name of the assistant
            instructions: System instructions for the assistant
            model: Model to use (default: gpt-4-turbo-preview)
            tools: List of tools to enable (code_interpreter, retrieval)
            file_ids: List of file IDs to attach
            metadata: Additional metadata
            functions: List of custom functions to make available
        """
        super().__init__(*args, **kwargs)
        
        # Initialize tools list with any provided functions
        self.tools = tools or []
        if functions:
            for func in functions:
                self.tools.append({
                    "type": "function",
                    "function": func
                })

        # Create the OpenAI Assistant
        self.client = OpenAI()
        self.assistant = self.client.beta.assistants.create(
            name=name,
            instructions=instructions,
            model=model,
            tools=self.tools,
            file_ids=file_ids or [],
            metadata=metadata or {}
        )
        
        # Store available functions
        self.available_functions: Dict[str, Callable] = {}
        
    def add_function(self, func: Callable, description: str, parameters: Dict[str, Any]) -> None:
        """Add a function that the assistant can call.
        
        Args:
            func: The function to make available to the assistant
            description: Description of what the function does
            parameters: JSON schema describing the function parameters
        """
        func_dict = {
            "name": func.__name__,
            "description": description,
            "parameters": parameters
        }
        
        # Add to tools list
        self.tools.append({
            "type": "function",
            "function": func_dict
        })
        
        # Store function reference
        self.available_functions[func.__name__] = func
        
        # Update assistant with new tools
        self.assistant = self.client.beta.assistants.update(
            assistant_id=self.assistant.id,
            tools=self.tools
        )

    def _handle_tool_calls(self, run, thread_id: str) -> None:
        """Handle any required tool calls during a run.
        
        This method processes any tool calls required by the assistant during execution.
        It extracts function calls, executes them with provided arguments, and submits
        the results back to the assistant.

        Args:
            run: The current run object from the OpenAI API
            thread_id: ID of the current conversation thread

        Returns:
            Updated run object after processing tool calls

        Raises:
            Exception: If there are errors executing the tool calls
        """
        while run.status == "requires_action":
            tool_calls = run.required_action.submit_tool_outputs.tool_calls
            tool_outputs = []
            
            for tool_call in tool_calls:
                if tool_call.type == "function":
                    # Get function details
                    function_name = tool_call.function.name
                    function_args = json.loads(tool_call.function.arguments)
                    
                    # Call function if available
                    if function_name in self.available_functions:
                        function_response = self.available_functions[function_name](**function_args)
                        tool_outputs.append({
                            "tool_call_id": tool_call.id,
                            "output": str(function_response)
                        })
            
            # Submit outputs back to the run
            run = self.client.beta.threads.runs.submit_tool_outputs(
                thread_id=thread_id,
                run_id=run.id,
                tool_outputs=tool_outputs
            )
            
            # Wait for processing
            run = self._wait_for_run(run)
        
        return run

    def _wait_for_run(self, run) -> Any:
        """Wait for a run to complete and handle any required actions.
        
        This method polls the OpenAI API to check the status of a run until it completes
        or fails. It handles intermediate states like required actions and implements
        exponential backoff.

        Args:
            run: The run object to monitor

        Returns:
            The completed run object

        Raises:
            Exception: If the run fails or expires
        """
        while True:
            run = self.client.beta.threads.runs.retrieve(
                thread_id=run.thread_id,
                run_id=run.id
            )
            
            if run.status == "completed":
                break
            elif run.status == "requires_action":
                run = self._handle_tool_calls(run, run.thread_id)
                if run.status == "completed":
                    break
            elif run.status in ["failed", "expired"]:
                raise Exception(f"Run failed with status: {run.status}")
                
            time.sleep(3)  # Wait 3 seconds before checking again
        
        return run

    def _ensure_thread(self):
        """Ensure a thread exists for the conversation.
        
        This method checks if there is an active thread for the current conversation.
        If no thread exists, it creates a new one. This maintains conversation context
        across multiple interactions.

        Side Effects:
            Sets self.thread if it doesn't exist
        """
        if not self.thread:
            self.thread = self.client.beta.threads.create()

    def add_message(self, content: str, file_ids: Optional[List[str]] = None) -> None:
        """Add a message to the thread.

        This method adds a new user message to the conversation thread. It ensures
        a thread exists before adding the message and handles file attachments.

        Args:
            content: The text content of the message to add
            file_ids: Optional list of file IDs to attach to the message. These must be
                     files that have been previously uploaded to OpenAI.

        Side Effects:
            Creates a new thread if none exists
            Adds the message to the thread in OpenAI's system
        """
        self._ensure_thread()
        self.client.beta.threads.messages.create(
            thread_id=self.thread.id,
            role="user",
            content=content,
            file_ids=file_ids or []
        )

    def _get_response(self) -> str:
        """Get the latest assistant response from the thread."""
        messages = self.client.beta.threads.messages.list(
            thread_id=self.thread.id,
            order="desc",
            limit=1
        )
        
        if not messages.data:
            return ""
            
        message = messages.data[0]
        if message.role == "assistant":
            return message.content[0].text.value
        return ""

    def run(self, task: str, *args, **kwargs) -> str:
        """Run a task using the OpenAI Assistant.

        Args:
            task: The task or prompt to send to the assistant
            
        Returns:
            The assistant's response as a string
        """
        self._ensure_thread()
        
        # Add the user message
        self.add_message(task)
        
        # Create and run the assistant
        run = self.client.beta.threads.runs.create(
            thread_id=self.thread.id,
            assistant_id=self.assistant.id,
            instructions=self.instructions
        )
        
        # Wait for completion
        run = self._wait_for_run(run)
        
        # Only get and return the response if run completed successfully
        if run.status == "completed":
            return self._get_response()
        return ""

    def call(self, task: str, *args, **kwargs) -> str:
        """Alias for run() to maintain compatibility with different agent interfaces."""
        return self.run(task, *args, **kwargs)