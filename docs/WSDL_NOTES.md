# Workday Calculated Field / Report Definition WSDL — Discovery Notes

## Service
- **Name**: `Core_Implementation_Service`
- **Services host**: `impl-services1.wd12.myworkday.com` (NOT the UI host `impl.wd12.myworkday.com`)
- **UI host**: `impl.wd12.myworkday.com`
- **SOAP endpoint**: `https://{services_host}/ccx/service/{tenant}/Core_Implementation_Service/{version}`
- **WSDL**: `https://impl-services1.wd12.myworkday.com/ccx/service/commitconsulting_dpt1/Core_Implementation_Service/v47.0?wsdl`
- **Tenant version**: v47.0 (confirmed max supported on this tenant — v48.0+ return HTTP 500)
- **Security domain**: Special OX Web Services (System functional area)

### Report_Metadata is not usable on this tenant
`Report_Metadata`'s WSDL also defines these operations at v47.0 and resolves
fine, but every call fails live with `SOAP-ENV:Client.validationError` —
"The web service or version is invalid for the requested operation" — even
with the ISU confirmed as a proper Integration System User with Special OX
Web Services and Custom Reports and Fields domain access granted and
activated. Confirmed via live testing (2026-07-30) that this isn't an auth,
IP, OAuth, version, or request-shape problem: the same credentials succeed
against `Staffing.Get_Workers` and against the identical
`Get_Calculated_Fields` operation on `Core_Implementation_Service`. Use
`Core_Implementation_Service` for all Get/Put calls in this project.

## Operations

| Operation | Direction | Description |
|-----------|-----------|-------------|
| `Get_Calculated_Fields` | Read | Fetch one or more calculated field definitions |
| `Put_Calculated_Field` | Write | Create or update a calculated field |
| `Get_Tenanted_Report_Definitions` | Read | Fetch one or more report definitions |
| `Put_Tenanted_Report_Definition` | Write | Create or update a report definition |
| `Get_Tenanted_Report_Definitions_Base` | Read | Lighter version (base fields only) |
| `Put_Tenanted_Report_Definition_Base` | Write | Write base report definition |

## Get_Calculated_Fields_RequestType
- `Request_References` (Calculated_Field_Request_ReferencesType) — fetch by WID reference
- `Request_Criteria` (Calculated_Field_Request_CriteriaType) — fetch by criteria (currently empty type = fetch all)
- `Response_Filter` (Response_FilterType) — pagination (Page, Count)
- `Response_Group` (Calculated_Field_Response_GroupType) — Include_Reference, Include_Calculated_Field_Data

## Put_Calculated_Field_RequestType
- `Calculated_Field_Reference` (Calculated_Field__Last_Entry_ObjectType) — optional, omit for create
- `Calculated_Field_Data` (Calculated_Field_DataType) — required

## Calculated_Field_DataType — 45 fields
### Base fields
- `Calculated_Field_Reference_ID`: string — the stable ID to use across tenants
- `Class_Name`: string — discriminator for field type
- `Name`: string
- `Description`: string (optional)
- `External_Field_Category_Reference`: External_Field_CategoryObjectType
- `External_Field_Usage_Reference`: External_Field_UsageObjectType
- `External_Field_Reference`: Business_ObjectObjectType — the business object this field is on
- `Intermediate_Calculation`: boolean
- `Do_Not_Use`: boolean
- `Option_Reference`: Calculated_Field_OptionObjectType
- `WQL_Alias`: string

### Sub-type fields (polymorphic — exactly one populated per field)
- `Arithmetic_Calculated_Field_Data`
- `Conditional_Expression_Calculated_Field_Data`
- `Concatenate_Calculated_Field_Data`
- `Convert_Currency_Calculated_Field_Data`
- `Date_Constant_Calculated_Field_Data`
- `Date_Difference_Calculated_Field_Data`
- `Extract_Single_Instance_Calculated_Field_Data`
- `Evaluate_Expression_Calculated_Field_Data`
- `Increment_or_Decrement_Date_Calculated_Field_Data`
- `Lookup_Single_Instance_Calculated_Field_Data`
- `Lookup_Value_As_Of_Date_Calculated_Field_Data`
- `Numeric_Constant_Calculated_Field_Data`
- `Text_Constant_Calculated_Field_Data`
- `Format_Date_Calculated_Field_Data`
- `Extract_Multi-Instance_Calculated_Field_Data`
- `Lookup_Org_Calculated_Field_Data`
- `Lookup_Org_Role_Assignments_Calculated_Field_Data`
- `Lookup_Range_Band_Calculated_Field_Data`
- `Count_Related_Instances_Calculated_Field_Data`
- `Sum_Related_Instances_Calculated_Field_Data`
- `Text_Substring_Calculated_Field_Data`
- `Text_Length_Calculated_Field_Data`
- `Lookup_Hierarchy_Rollup_Calculated_Field_Data`
- `Format_Number_Calculated_Field_Data`
- `Convert_Text_To_Number_Calculated_Field_Data`
- `Aggregate_Related_Instances_Calculated_Field_Data`
- `Lookup_Translated_Value_Data`
- `Build_Date_Calculated_Field_Data`
- `Lookup_Hierarchy_Calculated_Field_Data`
- `Prompt_Calculated_Field_Data`
- `Lookup_Date_Rollup_Calculated_Field_Data`
- `Format_Text_Calculated_Field_Data`
- `Lookup_Field_with_Prompts_Calculated_Field_Data`
- `Evaluate_Expression_Band_Calculated_Field_Data`

## Tenanted_Report_Definition_DataType — 77 fields (key ones)
- `Name`: string
- `Tenanted_Report_Definition_System_User_Reference`: System_UserObjectType — report owner
- `Tenanted_Report_Definition_Type_Reference`: Report_TypeObjectType
- `Report_Tag_Reference`: Report_TagObjectType
- `Enable_As_Worklet`: boolean
- `Web_Service_API_Version_Reference`: Web_Service_API_VersionObjectType
- `Web_Service_Include_Facets`: boolean
- `Data_Source_Reference`: Data_SourceObjectType
- `Formatting_Style__All__Reference`
- `Instructions`, `Comment`: string
- `Enable_Compare`, `Enable_Save_Parameters`, `Enable_Preferred_Currency`: boolean
- `Web_Service_Namespace_Suffix`: string
- `Tenanted_Report_Column_Data` — report columns (sub-type)
- `Tenanted_Report_Definition_Sub_Filter_Data` — filters
- `Tenanted_Report_Chart_Layout_Data` — chart config
- ... (77 total, full list in WSDL)

## Key architectural notes
1. **Dependency order**: calculated fields can reference other calculated fields via their sub-type data.
   Must topologically sort before Putting to destination.
2. **Reference IDs vs WIDs**: Use `Calculated_Field_Reference_ID` (stable string ID) not WID
   (tenant-specific integer) when cross-referencing between tenants.
3. **System_User_Reference on reports**: The owner field will differ between tenants.
   Tool must map source owner → destination owner or use the destination ISU as owner.
4. **Data_Source_Reference**: Data sources must exist in destination before report is imported.
   Tool should validate/warn if data source is missing.
