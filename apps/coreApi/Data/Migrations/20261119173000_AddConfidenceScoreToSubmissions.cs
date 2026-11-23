using Microsoft.EntityFrameworkCore.Migrations;

#nullable disable

namespace coreApi.Data.Migrations
{
    /// <inheritdoc />
    [Migration("20261119173000_AddConfidenceScoreToSubmissions")]
    public partial class AddConfidenceScoreToSubmissions : Migration
    {
        /// <inheritdoc />
        protected override void Up(MigrationBuilder migrationBuilder)
        {
            migrationBuilder.AddColumn<double>(
                name: "ConfidenceScorePercent",
                table: "Submissions",
                type: "double precision",
                nullable: true);
        }

        /// <inheritdoc />
        protected override void Down(MigrationBuilder migrationBuilder)
        {
            migrationBuilder.DropColumn(
                name: "ConfidenceScorePercent",
                table: "Submissions");
        }
    }
}
