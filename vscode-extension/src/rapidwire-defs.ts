export const sdkDefs = {
  "variables": [
    {
      "name": "sender",
      "type": "int",
      "doc": ""
    },
    {
      "name": "self_id",
      "type": "int",
      "doc": ""
    },
    {
      "name": "input_data",
      "type": "str",
      "doc": ""
    },
    {
      "name": "storage_str",
      "type": "Dict[str, str]",
      "doc": ""
    },
    {
      "name": "storage_int",
      "type": "Dict[str, int]",
      "doc": ""
    }
  ],
  "functions": [
    {
      "name": "output",
      "args": [
        {
          "name": "msg",
          "type": "str"
        }
      ],
      "returnType": "None",
      "doc": ""
    },
    {
      "name": "transfer",
      "args": [
        {
          "name": "to",
          "type": "int"
        },
        {
          "name": "amount",
          "type": "int"
        },
        {
          "name": "currency",
          "type": "int"
        }
      ],
      "returnType": "None",
      "doc": ""
    },
    {
      "name": "cancel",
      "args": [
        {
          "name": "reason",
          "type": "str"
        }
      ],
      "returnType": "None",
      "doc": ""
    },
    {
      "name": "exit",
      "args": [],
      "returnType": "None",
      "doc": ""
    },
    {
      "name": "sha256",
      "args": [
        {
          "name": "val",
          "type": "str"
        }
      ],
      "returnType": "str",
      "doc": ""
    },
    {
      "name": "random",
      "args": [
        {
          "name": "min_val",
          "type": "int"
        },
        {
          "name": "max_val",
          "type": "int"
        }
      ],
      "returnType": "int",
      "doc": ""
    },
    {
      "name": "get_balance",
      "args": [
        {
          "name": "user",
          "type": "int"
        },
        {
          "name": "currency",
          "type": "int"
        }
      ],
      "returnType": "int",
      "doc": ""
    },
    {
      "name": "concat",
      "args": [
        {
          "name": "a",
          "type": "str"
        },
        {
          "name": "b",
          "type": "str"
        }
      ],
      "returnType": "str",
      "doc": ""
    },
    {
      "name": "approve",
      "args": [
        {
          "name": "spender",
          "type": "int"
        },
        {
          "name": "amount",
          "type": "int"
        },
        {
          "name": "currency",
          "type": "int"
        }
      ],
      "returnType": "None",
      "doc": ""
    },
    {
      "name": "transfer_from",
      "args": [
        {
          "name": "sender",
          "type": "int"
        },
        {
          "name": "recipient",
          "type": "int"
        },
        {
          "name": "amount",
          "type": "int"
        },
        {
          "name": "currency",
          "type": "int"
        }
      ],
      "returnType": "Transaction",
      "doc": ""
    },
    {
      "name": "get_allowance",
      "args": [
        {
          "name": "owner",
          "type": "int"
        },
        {
          "name": "spender",
          "type": "int"
        },
        {
          "name": "currency",
          "type": "int"
        }
      ],
      "returnType": "int",
      "doc": ""
    },
    {
      "name": "get_currency",
      "args": [
        {
          "name": "currency_id",
          "type": "int"
        }
      ],
      "returnType": "Currency",
      "doc": ""
    },
    {
      "name": "get_transaction",
      "args": [
        {
          "name": "tx_id",
          "type": "int"
        }
      ],
      "returnType": "Transaction",
      "doc": ""
    },
    {
      "name": "create_claim",
      "args": [
        {
          "name": "payer",
          "type": "int"
        },
        {
          "name": "amount",
          "type": "int"
        },
        {
          "name": "currency",
          "type": "int"
        },
        {
          "name": "desc",
          "type": "str"
        }
      ],
      "returnType": "Claim",
      "doc": ""
    },
    {
      "name": "pay_claim",
      "args": [
        {
          "name": "claim_id",
          "type": "int"
        }
      ],
      "returnType": "Transaction",
      "doc": ""
    },
    {
      "name": "cancel_claim",
      "args": [
        {
          "name": "claim_id",
          "type": "int"
        }
      ],
      "returnType": "Claim",
      "doc": ""
    },
    {
      "name": "execute",
      "args": [
        {
          "name": "destination_id",
          "type": "int"
        },
        {
          "name": "input_data",
          "type": "str"
        }
      ],
      "returnType": "str",
      "doc": ""
    },
    {
      "name": "discord_send",
      "args": [
        {
          "name": "guild_id",
          "type": "int"
        },
        {
          "name": "channel_id",
          "type": "int"
        },
        {
          "name": "message",
          "type": "str"
        }
      ],
      "returnType": "int",
      "doc": ""
    },
    {
      "name": "discord_role_add",
      "args": [
        {
          "name": "user_id",
          "type": "int"
        },
        {
          "name": "guild_id",
          "type": "int"
        },
        {
          "name": "role_id",
          "type": "int"
        }
      ],
      "returnType": "int",
      "doc": ""
    },
    {
      "name": "has_role",
      "args": [
        {
          "name": "user_id",
          "type": "int"
        },
        {
          "name": "guild_id",
          "type": "int"
        },
        {
          "name": "role_id",
          "type": "int"
        }
      ],
      "returnType": "bool",
      "doc": ""
    },
    {
      "name": "length",
      "args": [
        {
          "name": "val",
          "type": "Any"
        }
      ],
      "returnType": "int",
      "doc": ""
    }
  ],
  "classes": [
    {
      "name": "Currency",
      "doc": "",
      "fields": [
        {
          "name": "currency_id",
          "type": "int"
        },
        {
          "name": "name",
          "type": "str"
        },
        {
          "name": "symbol",
          "type": "str"
        },
        {
          "name": "issuer_id",
          "type": "int"
        },
        {
          "name": "supply",
          "type": "int"
        },
        {
          "name": "minting_renounced",
          "type": "bool"
        },
        {
          "name": "delete_requested_at",
          "type": "Optional[int]"
        },
        {
          "name": "daily_interest_rate",
          "type": "int"
        },
        {
          "name": "new_daily_interest_rate",
          "type": "Optional[int]"
        },
        {
          "name": "rate_change_requested_at",
          "type": "Optional[int]"
        }
      ]
    },
    {
      "name": "Transaction",
      "doc": "",
      "fields": [
        {
          "name": "transfer_id",
          "type": "int"
        },
        {
          "name": "execution_id",
          "type": "Optional[int]"
        },
        {
          "name": "source_id",
          "type": "int"
        },
        {
          "name": "dest_id",
          "type": "int"
        },
        {
          "name": "currency_id",
          "type": "int"
        },
        {
          "name": "amount",
          "type": "int"
        },
        {
          "name": "timestamp",
          "type": "int"
        }
      ]
    },
    {
      "name": "Claim",
      "doc": "",
      "fields": [
        {
          "name": "claim_id",
          "type": "int"
        },
        {
          "name": "claimant_id",
          "type": "int"
        },
        {
          "name": "payer_id",
          "type": "int"
        },
        {
          "name": "currency_id",
          "type": "int"
        },
        {
          "name": "amount",
          "type": "int"
        },
        {
          "name": "status",
          "type": "str"
        },
        {
          "name": "created_at",
          "type": "int"
        },
        {
          "name": "description",
          "type": "Optional[str]"
        }
      ]
    }
  ]
};
